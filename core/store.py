"""Durable storage for a process that is expected to stay up for months.

Two artefacts:

* ``data/audit.jsonl``  - append-only, hash-chained security log. Never rewritten,
  only rotated when it grows past ``MAX_AUDIT_BYTES``.
* ``data/state.json``   - atomic snapshot of entities, tiers, paper account and
  open positions, written every few seconds and on shutdown.

Everything is plain JSON on purpose: a 24/7 box should be debuggable with `cat`.
"""
from __future__ import annotations

import json
import os
import threading
import time

from core.config import ROOT

DATA_DIR = os.path.join(ROOT, "data")
LOG_DIR = os.path.join(ROOT, "logs")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
RECOVERY_FILE = os.path.join(DATA_DIR, "recovery.key")
PID_FILE = os.path.join(DATA_DIR, "arena.pid")
MAX_AUDIT_BYTES = 8 * 1024 * 1024


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, timeout=8, text=True)
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return True     # cannot prove it is dead: assume alive, stay read-only
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Store:
    def __init__(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        self._lock = threading.Lock()
        self._warned_secondary = False
        self._owner: bool | None = None
        self._checked_at = 0.0

    # ------------------------------------------------------------------ ownership
    def claim_writer(self) -> bool:
        """Mark this process as the audit writer.

        The hash chain is only meaningful with a single writer: a second process
        appending with its own counter forks the sequence. If a live PID already
        owns the file, this process becomes a read-only secondary and stops
        persisting audit lines (it still keeps them in memory).
        """
        now = time.time()
        if self._owner is not None and now - self._checked_at < 30:
            return self._owner
        self._checked_at = now
        owner = False
        try:
            if os.path.exists(PID_FILE):
                raw = open(PID_FILE, "r", encoding="utf-8").read().strip()
                other = int(raw or 0)
                if other and other != os.getpid() and _pid_alive(other):
                    owner = False
                else:
                    owner = True
            else:
                owner = True
            if owner:
                with open(PID_FILE, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
        except (OSError, ValueError):
            owner = False
        self._owner = owner
        return owner

    def release_writer(self) -> None:
        try:
            if os.path.exists(PID_FILE):
                if open(PID_FILE, "r", encoding="utf-8").read().strip() == str(os.getpid()):
                    os.remove(PID_FILE)
        except OSError:
            pass

    # ------------------------------------------------------------------ audit
    def append_audit(self, entry: dict) -> None:
        if not self.claim_writer():
            if not self._warned_secondary:
                self._warned_secondary = True
                print("  [audit] another live process owns data/audit.jsonl - "
                      "this process is read-only for the chain")
            return
        line = json.dumps(entry, sort_keys=True) + "\n"
        with self._lock:
            self._rotate_audit_if_needed()
            with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)

    def quarantine_audit(self) -> str:
        """Move a forked/corrupt chain aside instead of rewriting history."""
        if not os.path.exists(AUDIT_FILE):
            return ""
        dest = os.path.join(DATA_DIR, f"audit.quarantine-{int(time.time())}.jsonl")
        try:
            os.replace(AUDIT_FILE, dest)
        except OSError:
            return ""
        return dest

    def _rotate_audit_if_needed(self) -> None:
        try:
            if os.path.getsize(AUDIT_FILE) > MAX_AUDIT_BYTES:
                os.replace(AUDIT_FILE, os.path.join(DATA_DIR, f"audit.{int(time.time())}.jsonl"))
        except OSError:
            pass

    def load_audit_tail(self, limit: int = 400) -> list:
        if not os.path.exists(AUDIT_FILE):
            return []
        with self._lock:
            with open(AUDIT_FILE, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def count_audit_lines(self) -> int:
        if not os.path.exists(AUDIT_FILE):
            return 0
        with open(AUDIT_FILE, "rb") as fh:
            return sum(1 for _ in fh)

    # ------------------------------------------------------------------ recovery
    def ensure_recovery_key(self) -> str:
        """Break-glass token. Created once, readable only by this OS account.

        It is deliberately *not* served over HTTP: recovering a blocked identity
        requires filesystem access to the host, which is the same trust level as
        editing the code. That keeps the block meaningful while still leaving a
        way back in.
        """
        import secrets
        with self._lock:
            if os.path.exists(RECOVERY_FILE):
                try:
                    with open(RECOVERY_FILE, "r", encoding="utf-8") as fh:
                        token = fh.read().strip()
                    if len(token) >= 32:
                        return token
                except OSError:
                    pass
            token = secrets.token_urlsafe(32)
            with open(RECOVERY_FILE, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            try:
                os.chmod(RECOVERY_FILE, 0o600)
            except OSError:
                pass
            if os.name == "nt":
                # Windows ignores POSIX bits: strip inheritance, grant only this user
                import getpass
                import subprocess
                try:
                    user = os.environ.get("USERNAME") or getpass.getuser()
                    subprocess.run(["icacls", RECOVERY_FILE, "/inheritance:r",
                                    "/grant:r", f"{user}:F"],
                                   capture_output=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass
            return token

    def read_recovery_key(self) -> str:
        try:
            with open(RECOVERY_FILE, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""

    # ------------------------------------------------------------------ state
    def save_state(self, payload: dict) -> None:
        tmp = STATE_FILE + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, default=str)
            os.replace(tmp, STATE_FILE)

    def load_state(self) -> dict:
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (ValueError, OSError):
            # a half-written snapshot must never stop the agent from booting
            return {}


STORE = Store()
