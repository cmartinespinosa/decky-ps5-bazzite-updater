import asyncio
import hashlib
import json
import os
import re
import shutil

import decky

REPO = "VivaLaVent/ps5-bazzite-deck"
VERSION_FILE = "/etc/ps5-bazzite-deck-version"
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+[A-Za-z0-9.-]*$")


def _env():
    # Decky's bundled Python sets LD_LIBRARY_PATH, which breaks system binaries like curl
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return env


async def _run(*cmd, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


def _release(r):
    return {"tag": r["tag_name"], "name": r.get("name") or r["tag_name"],
            "published": r.get("published_at"), "body": r.get("body") or "",
            "url": r.get("html_url"), "prerelease": r.get("prerelease", False)}


def pick_releases(releases):
    """Latest stable, plus the latest pre-release only if it is newer than that stable."""
    rs = [r for r in releases if not r.get("draft") and TAG_RE.match(r.get("tag_name", ""))]
    rs.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    stable = next((r for r in rs if not r.get("prerelease")), None)
    pre = next((r for r in rs if r.get("prerelease")), None)
    if pre and stable and (pre.get("published_at") or "") < (stable.get("published_at") or ""):
        pre = None
    return (_release(stable) if stable else None, _release(pre) if pre else None)


def read_sha256(text):
    return text.split()[0].strip().lower() if text.strip() else ""


class Plugin:
    async def get_status(self):
        try:
            installed = open(VERSION_FILE).read().strip()
        except OSError:
            installed = "unknown"
        code, out = await _run("curl", "-fsSL", "--max-time", "20",
                               "-H", "Accept: application/vnd.github+json",
                               f"https://api.github.com/repos/{REPO}/releases?per_page=15")
        if code != 0:
            return {"installed": installed, "error": "Can't reach GitHub"}
        try:
            stable, pre = pick_releases(json.loads(out))
        except (ValueError, KeyError, TypeError):
            return {"installed": installed, "error": "Unexpected answer from GitHub"}
        return {"installed": installed, "stable": stable, "pre": pre}

    async def update(self, tag: str):
        """Download, verify and install one release's update bundle; streams log lines as events."""
        if not TAG_RE.match(tag or ""):
            return {"ok": False, "error": f"Invalid version {tag!r}"}

        async def log(line):
            decky.logger.info(line)
            await decky.emit("update_log", line)

        work = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "update")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        name = f"ps5-bazzite-deck-{tag}-update"
        url = f"https://github.com/{REPO}/releases/download/{tag}/{name}.tar.xz"
        tar = os.path.join(work, f"{name}.tar.xz")

        await log(f"Downloading {tag}…")
        code, out = await _run("curl", "-fsSL", "--max-time", "60", "-o", tar + ".sha256", url + ".sha256")
        if code != 0:
            return {"ok": False, "error": f"{tag} has no update bundle"}
        code, out = await _run("curl", "-fsSL", "--max-time", "900", "-o", tar, url)
        if code != 0:
            return {"ok": False, "error": "Download failed", "log": out}

        expected = read_sha256(open(tar + ".sha256").read())
        h = hashlib.sha256()
        with open(tar, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != expected:
            return {"ok": False, "error": "SHA256 doesn't match. Nothing was changed."}
        await log("SHA256 OK")

        code, out = await _run("tar", "xJf", tar, "-C", work)
        if code != 0:
            return {"ok": False, "error": "Couldn't unpack the bundle", "log": out}
        script = os.path.join(work, name, "update.sh")
        if not os.path.isfile(script):
            return {"ok": False, "error": "update.sh missing from the bundle"}

        await log("Running update.sh…")
        proc = await asyncio.create_subprocess_exec(
            "bash", script, cwd=os.path.dirname(script), env=_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        lines = []
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            lines.append(line)
            await log(line)
        code = await proc.wait()
        done = any(l.startswith("DONE:") for l in lines)
        if code != 0 or not done:
            return {"ok": False, "error": f"update.sh failed (exit {code})", "log": "\n".join(lines[-15:])}
        undo = next((l for l in lines if l.startswith("Undo:")), "")
        return {"ok": True, "undo": undo}

    async def poweroff(self):
        await _run("systemctl", "poweroff")

    async def _main(self):
        decky.logger.info("PS5 Updates loaded")

    async def _unload(self):
        pass
