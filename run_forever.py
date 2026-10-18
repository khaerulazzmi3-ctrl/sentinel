#!/usr/bin/env python3
"""Supervisor: keeps SENTINEL ARENA running 24/7.

    python run_forever.py [--public] [--port 8787]

Spawns ``run.py`` as a child, restarts it if it exits, with exponential backoff
so a crash loop cannot spin the CPU. Any extra arguments are forwarded to the
server. Logs to ``logs/supervisor.log`` and stdout.

This is what the Windows scheduled task starts, and what you run in a terminal
or under systemd / supervisor on Linux.
"""
from __future__ import annotations

import datetime
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "supervisor.log")
MAX_LOG_BYTES = 2 * 1024 * 1024

_child = None
_stopping = False


def log(msg: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} [supervisor] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def stop(signum, frame) -> None:
    global _stopping
    _stopping = True
    log(f"signal {signum} received, stopping child")
    if _child and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _child.kill()


def main() -> int:
    global _child
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError):
            pass

    args = [sys.executable, os.path.join(ROOT, "run.py")] + sys.argv[1:]
    backoff = 1
    log(f"supervisor online (python {sys.version.split()[0]})")
    while not _stopping:
        started = time.time()
        log("starting: " + " ".join(args[1:]))
        try:
            _child = subprocess.Popen(args, cwd=ROOT)
        except Exception as exc:
            log(f"failed to spawn: {exc}")
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)
            continue

        code = _child.wait()
        uptime = time.time() - started
        if _stopping:
            break

        if code == 0 and uptime < 15:
            log("child exited cleanly (port already served by another instance) - supervisor stopping")
            break
        if uptime > 120:
            backoff = 1          # it was healthy for a while: reset the ladder
        log(f"child exited code={code} after {uptime:.0f}s; restarting in {backoff}s")
        for _ in range(backoff):
            if _stopping:
                break
            time.sleep(1)
        backoff = min(backoff * 2, 60)

    log("supervisor stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
