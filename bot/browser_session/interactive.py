"""Operator-only interactive session: log a profile in or clear a checkpoint.

Run inside the ``browser`` container (``scripts/browser_login.sh`` does this).
It takes the same Redis lease and profile ``flock`` as any collector, so it can
never share a profile with automated work, then opens the page on the
container's virtual display and exposes that display through noVNC only for
the length of the session, behind a one-time password.

The noVNC port is published on the host's loopback interface only; reach it
through an SSH tunnel or ``tailscale serve``. Nothing here is reachable from
the public internet, and nothing runs once the session ends.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import signal
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

import redis.asyncio as redis

from .manager import BrowserSessionManager
from .models import ProfileRequest
from .settings import BrowserSessionSettings

VNC_PORT = 5900
NOVNC_PORT = 6080
NOVNC_WEB_ROOT = "/usr/share/novnc"
MAX_MINUTES = 60
VIEWER_LOGS = ("/tmp/x11vnc.log", "/tmp/websockify.log")


def _start_viewer(password: str) -> list[subprocess.Popen[bytes]]:
    """Start x11vnc (container-local) and noVNC for the current display only."""
    display = os.environ.get("DISPLAY", ":99")
    fd, path = tempfile.mkstemp(prefix="vncpass-")
    with os.fdopen(fd, "w") as handle:
        handle.write(password + "\n")
    os.chmod(path, 0o600)
    # Errors go to /tmp so a viewer that will not start can be diagnosed with
    # `docker compose exec browser cat /tmp/x11vnc.log /tmp/websockify.log`.
    with open(VIEWER_LOGS[0], "ab") as vnc_log, open(VIEWER_LOGS[1], "ab") as novnc_log:
        vnc = subprocess.Popen(
            # `rm:` makes x11vnc delete the password file as soon as it has read it.
            ["x11vnc", "-display", display, "-localhost", "-rfbport", str(VNC_PORT),
             "-passwdfile", f"rm:{path}", "-forever", "-shared", "-quiet", "-noxdamage"],
            stdout=subprocess.DEVNULL, stderr=vnc_log,
        )
        novnc = subprocess.Popen(
            ["websockify", "--web", NOVNC_WEB_ROOT, f"0.0.0.0:{NOVNC_PORT}", f"localhost:{VNC_PORT}"],
            stdout=subprocess.DEVNULL, stderr=novnc_log,
        )
    return [vnc, novnc]


def _log_tail(path: str, limit: int = 300) -> str:
    try:
        return Path(path).read_bytes()[-limit:].decode(errors="replace").strip()
    except OSError:
        return ""


async def _wait_viewer(processes: list[subprocess.Popen[bytes]], timeout: float = 15.0) -> None:
    """Return once noVNC accepts connections; raise if a viewer process died first.

    Starting the processes returns at once, but websockify needs a moment to
    bind. Reporting the window as open before that made the first page load
    fail with "the browser is not running".
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for process, log_path in zip(processes, VIEWER_LOGS, strict=False):
            if process.poll() is not None:
                raise RuntimeError(f"{Path(log_path).stem} exited ({process.returncode}): {_log_tail(log_path)}")
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", NOVNC_PORT), timeout=1)
        except (OSError, TimeoutError):
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(f"noVNC did not start listening within {timeout:.0f} s") from None
            await asyncio.sleep(0.25)
            continue
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        return


def _stop(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        with suppress(ProcessLookupError):
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def run(profile_id: str, platform: str, url: str, minutes: int) -> None:
    settings = BrowserSessionSettings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    # Renew well inside the lease; idle release is disabled by making the
    # idle window the whole session, since a human drives this browser.
    manager = BrowserSessionManager(
        client, profile_root=settings.profile_root, screenshot_root=settings.screenshot_root,
        lease_seconds=settings.lease_seconds, renew_seconds=settings.lease_renew_seconds,
        idle_seconds=max(minutes * 60 + 60, settings.lease_renew_seconds + 1),
    )
    handle = await manager.acquire(ProfileRequest(profile_id, profile_id, platform))
    viewer: list[subprocess.Popen[bytes]] = []
    try:
        # The same host-scoped navigation primitive collectors use; its result
        # is irrelevant here, only the page it leaves open.
        await manager.snapshot(handle, url, timeout_ms=60_000)
        password = secrets.token_urlsafe(9)[:8]  # VNC uses at most 8 characters
        viewer = _start_viewer(password)
        await _wait_viewer(viewer)
        print(
            f"\nBrowser for profile {profile_id} is open at {url}\n"
            f"noVNC password for this session: {password}\n"
            f"Log in or clear the checkpoint, then press Ctrl+C here. "
            f"The session closes by itself after {minutes} minutes.\n",
            flush=True,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
    finally:
        _stop(viewer)
        # Closing the persistent context flushes cookies into the profile dir.
        await manager.release(handle)
        await client.aclose()
    print("Session closed; the profile directory keeps the login.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--platform", default="facebook", choices=["facebook", "instagram", "tiktok", "linkedin", "website"])
    parser.add_argument("--url", default="https://www.facebook.com/")
    parser.add_argument("--minutes", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.minutes <= MAX_MINUTES:
        raise SystemExit(f"--minutes must be between 1 and {MAX_MINUTES}")
    if not args.url.startswith("https://"):
        raise SystemExit("--url must be HTTPS")
    Path(os.environ.get("BROWSER_PROFILE_ROOT", "/profiles")).mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.profile_id, args.platform, args.url, args.minutes))


if __name__ == "__main__":
    main()
