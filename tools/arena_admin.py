#!/usr/bin/env python3
"""Break-glass administration for SENTINEL ARENA.

Use this when the security agent has blocked the machine you are sitting at, so
you cannot even reach the login page. It reads ``data/recovery.key`` - a token
that only exists on this host's filesystem - and asks the running service to
restore access. If the service is not running it edits the saved state directly
and the change is loaded on the next start.

    python tools/arena_admin.py status
    python tools/arena_admin.py entities
    python tools/arena_admin.py unblock 203.0.113.44
    python tools/arena_admin.py promote 203.0.113.44 ADMIN
    python tools/arena_admin.py promote 203.0.113.44 DIREKTUR --reason "co-founder"
    python tools/arena_admin.py block 203.0.113.44 --reason "manual containment"
    python tools/arena_admin.py key

Anyone who can run this already has filesystem access to the host, which is the
same trust level as editing the source. Every action is written to the audit
chain as actor RECOVERY / BREAK_GLASS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "state.json")
RECOVERY_FILE = os.path.join(DATA, "recovery.key")
CONFIG_FILE = os.path.join(ROOT, "config.json")

TIER_ORDER = ["BLOCKED", "BIASA", "ADMIN", "DIREKTUR"]


def port() -> int:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return int(json.load(fh).get("system", {}).get("port", 8787))
    except Exception:
        return 8787


def token() -> str:
    try:
        with open(RECOVERY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def call(path: str, payload: dict | None = None, timeout: float = 8.0):
    url = f"http://127.0.0.1:{port()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "ArenaAdmin/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode() or "{}")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, default=str)
    os.replace(tmp, STATE_FILE)


def find(state: dict, ident: str) -> dict | None:
    for ent in (state.get("security") or {}).get("entities") or []:
        if ent.get("eid") == ident or ent.get("ip") == ident:
            return ent
    return None


def offline_change(ident: str, tier: str, reason: str) -> int:
    # the server snapshots every 15s: a fresh file means it is alive and would
    # overwrite us on the next tick, so refuse instead of silently losing the edit
    try:
        age = time.time() - os.path.getmtime(STATE_FILE)
    except OSError:
        age = 999
    if age < 20:
        print("! service tampak hidup (state baru ditulis %.0fs lalu) tapi API tidak menjawab." % age)
        print("  Periksa port/firewall. Tidak menulis offline agar perubahan tidak tertimpa.")
        return 1
    state = load_state()
    ent = find(state, ident)
    if ent is None:
        print(f"! identitas '{ident}' tidak ditemukan di {STATE_FILE}")
        return 1
    old = ent.get("tier")
    ent["tier"] = tier
    if tier == "BLOCKED":
        ent["blocked_at"] = time.time()
        ent["block_reason"] = reason or "Break-glass block"
        ent["quarantined"] = True
    else:
        ent["blocked_at"] = None
        ent["block_reason"] = ""
        ent["quarantined"] = False
        ent["score"] = 0
    ent.setdefault("history", []).append(
        {"ts": int(time.time() * 1000), "event": f"OFFLINE {old}->{tier}",
         "reason": reason, "actor": "RECOVERY"})
    save_state(state)
    print(f"  {ident}: {old} -> {tier}  (ditulis offline ke data/state.json)")
    print("  Server sedang mati; perubahan dibaca saat start berikutnya.")
    print("  Catatan: jika server ternyata hidup, jalankan ulang perintah ini lewat API.")
    return 0


def change(ident: str, tier: str, reason: str) -> int:
    tok = token()
    if not tok:
        print("! data/recovery.key tidak ada. Jalankan server sekali agar token dibuat,")
        print("  atau buat manual: python -c \"import secrets;print(secrets.token_urlsafe(32))\" > data/recovery.key")
        return 1
    try:
        status, body = call("/api/recovery", {"token": tok, "target": ident,
                                              "tier": tier, "reason": reason})
    except urllib.error.HTTPError as exc:
        # the server answered and refused: never fall back to editing the file
        try:
            body = json.loads(exc.read().decode() or "{}")
        except Exception:
            body = {}
        print(f"! ditolak server: {body.get('error') or ('HTTP ' + str(exc.code))}")
        return 1
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return offline_change(ident, tier, reason)
    if status == 200 and body.get("ok"):
        print(f"  {ident}: {body['from']} -> {body['to']}  (break-glass, tercatat di audit)")
        return 0
    print(f"! gagal: {body.get('error') or status}")
    return 1


def cmd_status() -> int:
    state = load_state()
    sec = state.get("security") or {}
    trd = state.get("trading") or {}
    ents = sec.get("entities") or []
    blocked = [e for e in ents if e.get("tier") == "BLOCKED"]
    print("SENTINEL ARENA")
    print(f"  state terakhir : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(state.get('saved_at', 0)))}")
    print(f"  entitas        : {len(ents)}  (diblokir {len(blocked)})")
    print(f"  audit seq      : {sec.get('seq', 0)}")
    print(f"  mode           : {trd.get('mode', '?')}   paused={trd.get('paused')} killed={trd.get('killed')}")
    acct = (trd.get("broker") or {})
    if acct:
        print(f"  saldo paper    : ${acct.get('balance', 0):,.2f}  posisi {len(acct.get('positions') or [])}")
    try:
        _, h = call("/api/health", timeout=3)
        print(f"  service        : ONLINE  heartbeat {h.get('heartbeat_age_s')}s  uptime {h.get('uptime_s')}s")
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            print("  service        : ONLINE, tapi identitas mesin ini sedang DIBLOKIR")
            print("                   pulihkan: python tools/arena_admin.py unblock 127.0.0.1")
        else:
            print(f"  service        : HTTP {exc.code}")
    except Exception:
        print("  service        : OFFLINE (perintah akan ditulis langsung ke state)")
    return 0


def cmd_entities() -> int:
    state = load_state()
    ents = (state.get("security") or {}).get("entities") or []
    if not ents:
        print("belum ada entitas")
        return 0
    ents.sort(key=lambda e: (TIER_ORDER.index(e.get("tier", "BIASA")) if e.get("tier") in TIER_ORDER else 9,
                             -e.get("score", 0)))
    print(f"{'TIER':10s} {'SKOR':>5s}  {'IP':16s} {'LABEL':20s} ALASAN")
    for e in ents:
        print(f"{e.get('tier',''):10s} {e.get('score',0):5d}  {e.get('ip',''):16s} "
              f"{e.get('label','')[:20]:20s} {(e.get('block_reason') or '')[:44]}")
    return 0


def cmd_key() -> int:
    tok = token()
    if not tok:
        print("belum ada token; jalankan server sekali")
        return 1
    print(f"file  : {RECOVERY_FILE}")
    print(f"token : {tok}")
    print("\nPERINGATAN: token ini setara kredensial root untuk arena.")
    print("Siapa pun yang membacanya bisa membuka blokir dan mengangkat tier.")
    print("Jangan commit, jangan kirim lewat chat, jangan taruh di folder bersama.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SENTINEL ARENA break-glass admin")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status", help="ringkasan state + status service")
    sub.add_parser("entities", help="daftar identitas dan tier")
    sub.add_parser("key", help="tampilkan token pemulihan")
    for name, helptext in (("unblock", "kembalikan identitas ke BIASA"),
                           ("block", "blokir identitas"),
                           ("promote", "angkat identitas ke ADMIN/DIREKTUR")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("target", help="IP atau entity id")
        if name == "promote":
            p.add_argument("tier", choices=["BIASA", "ADMIN", "DIREKTUR"])
        p.add_argument("--reason", default="break-glass recovery", help="alasan (masuk audit)")
    args = ap.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "entities":
        return cmd_entities()
    if args.cmd == "key":
        return cmd_key()
    if args.cmd == "unblock":
        return change(args.target, "BIASA", args.reason)
    if args.cmd == "block":
        return change(args.target, "BLOCKED", args.reason)
    if args.cmd == "promote":
        return change(args.target, args.tier, args.reason)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
