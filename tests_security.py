"""End-to-end checks for the tier ladder, auto-block and audit chain."""
import json
import urllib.request

BASE = "http://127.0.0.1:8787"
ADM = "ADM-HGZF-RGT6-X4H9"
DIR = "DIR-R69E-T72D-8GL3"
GUEST = "USR-7EZF-FR5L-ZQHV"


def call(path, payload=None, cid="127.0.0.1", method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Client-Id", cid)
    req.add_header("User-Agent", "ArenaOps/1.0")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def show(label, status, body, expect):
    ok = "PASS" if status == expect else "FAIL"
    msg = body.get("error") or body.get("to") or body.get("tier") or ""
    print(f"[{ok}] {label:48s} http={status} expect={expect}  {str(msg)[:64]}")


# A previous run can leave 127.0.0.1 blocked: the gate test deliberately fails
# logins, and 5 failures trip the auto-block. Recover with the break-glass token
# so the suite always starts from a known state - and prove that path works too.
import os
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recovery.key")
try:
    TOKEN = open(TOKEN_PATH, encoding="utf-8").read().strip()
except OSError:
    TOKEN = ""
if TOKEN:
    call("/api/recovery", {"token": TOKEN, "target": "127.0.0.1", "tier": "ADMIN",
                           "reason": "suite start: clear any leftover lockout"})

# reset demo entities so the suite is idempotent
call("/api/security/act", {"key": DIR, "target": "203.0.113.44", "tier": "BLOCKED", "reason": "suite reset"})
call("/api/security/act", {"key": DIR, "target": "198.51.100.7", "tier": "BIASA", "reason": "suite reset"})

print("== auto-block on intrusion ==")
st, body = call("/api/security/attack", {"key": ADM, "kind": "sqli"})
r = body["result"]
print(f"[{'PASS' if r['decision']['blocked'] else 'FAIL'}] sqli auto-blocked, tier={r['entity']['tier']}, "
      f"detections={len(r['decision']['detections'])}, heal_steps={len(r['heal_actions'])}")

st, body = call("/api/security/attack", {"key": ADM, "kind": "bruteforce"})
print(f"[{'PASS' if body['result']['decision']['blocked'] else 'FAIL'}] brute force auto-blocked")

print("\n== blocked identity is denied everywhere ==")
for path in ("/api/state", "/", "/api/health"):
    st, body = call(path, cid="192.0.2.91")
    print(f"[{'PASS' if st == 403 else 'FAIL'}] blocked entity -> {path:14s} http={st}")

print("\n== tier ladder authority ==")
st, body = call("/api/security/act", {"key": ADM, "target": "203.0.113.44", "tier": "BIASA",
                                      "reason": "verified by ops"})
show("ADMIN revokes BLOCKED -> BIASA", st, body, 200)
st, body = call("/api/security/act", {"key": ADM, "target": "203.0.113.44", "tier": "ADMIN",
                                      "reason": "shift lead"})
show("ADMIN raises BIASA -> ADMIN", st, body, 200)
st, body = call("/api/security/act", {"key": ADM, "target": "203.0.113.44", "tier": "DIREKTUR",
                                      "code": "ARENA-DIRECTOR-77"})
show("ADMIN cannot appoint DIREKTUR", st, body, 403)
st, body = call("/api/security/act", {"key": ADM, "target": "10.0.0.1", "tier": "BIASA"})
show("ADMIN cannot touch a DIREKTUR", st, body, 403)
st, body = call("/api/security/act", {"key": GUEST, "target": "203.0.113.44", "tier": "BIASA"})
show("BIASA has no authority", st, body, 403)
st, body = call("/api/security/act", {"key": DIR, "target": "198.51.100.7", "tier": "ADMIN",
                                      "reason": "shift lead"})
show("DIREKTUR raises BIASA -> ADMIN", st, body, 200)
st, body = call("/api/security/act", {"key": DIR, "target": "198.51.100.7", "tier": "DIREKTUR"})
show("DIREKTUR needs confirmation code", st, body, 403)
st, body = call("/api/security/act", {"key": DIR, "target": "198.51.100.7", "tier": "DIREKTUR",
                                      "code": "ARENA-DIRECTOR-77", "reason": "co-founder"})
show("DIREKTUR + code -> DIREKTUR", st, body, 200)
st, body = call("/api/security/act", {"key": ADM, "target": "198.51.100.7", "tier": "BIASA"})
show("ADMIN cannot demote a DIREKTUR", st, body, 403)
st, body = call("/api/security/act", {"key": ADM, "target": "203.0.113.44", "tier": "BLOCKED",
                                      "reason": "manual containment"})
show("ADMIN can still block", st, body, 200)

print("\n== gate: key + tier must match ==")
st, body = call("/api/login", {"key": DIR}, cid="gate-probe")
show("login DIREKTUR key, no tier claim", st, body, 200)
st, body = call("/api/login", {"key": GUEST, "tier": "DIREKTUR"}, cid="gate-probe")
show("BIASA key claiming DIREKTUR", st, body, 403)
st, body = call("/api/login", {"key": ADM, "tier": "ADMIN"}, cid="gate-probe")
show("ADMIN key + ADMIN tier", st, body, 200)
st, body = call("/api/login", {"key": "NOPE-0000-0000-0000", "tier": "BIASA"}, cid="gate-probe")
show("unknown key rejected", st, body, 401)

print("\n== trading controls ==")
st, body = call("/api/trading/control", {"key": GUEST, "action": "pause"})
show("BIASA cannot pause trading", st, body, 403)
st, body = call("/api/trading/control", {"key": ADM, "action": "pause"})
show("ADMIN pauses trading", st, body, 200)
st, body = call("/api/trading/control", {"key": ADM, "action": "resume"})
show("ADMIN resumes trading", st, body, 200)

print("\n== audit chain ==")
st, body = call("/api/audit")
print(f"[{'PASS' if body['chain_ok'] else 'FAIL'}] hash chain valid, {len(body['entries'])} entries")
for e in body["entries"][:5]:
    print(f"   #{e['seq']:3d} {e['actor']:8s} {e['action']:14s} {e['target']:14s} {e['detail'][:52]}")

print("\n== break-glass recovery ==")
import os
tok_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recovery.key")
try:
    token = open(tok_path, encoding="utf-8").read().strip()
except OSError:
    token = ""
print(f"[{'PASS' if token else 'FAIL'}] recovery token exists on disk (len={len(token)})")

call("/api/security/act", {"key": DIR, "target": "127.0.0.1", "tier": "BLOCKED",
                           "reason": "lockout drill"}, cid="ops-console")
st, body = call("/api/login", {"key": DIR})
print(f"[{'PASS' if st == 403 else 'FAIL'}] blocked operator cannot log in, even with DIREKTUR key")
st, body = call("/api/recovery", {"token": "wrong-token", "target": "127.0.0.1", "tier": "BIASA"})
print(f"[{'PASS' if st == 403 else 'FAIL'}] wrong break-glass token rejected")
st, body = call("/api/recovery", {"token": token, "target": "127.0.0.1", "tier": "BIASA",
                                  "reason": "lockout drill recovery"})
print(f"[{'PASS' if st == 200 and body.get('ok') else 'FAIL'}] break-glass unblock succeeds while blocked")
st, body = call("/api/login", {"key": DIR})
print(f"[{'PASS' if st == 200 else 'FAIL'}] login restored after recovery")
call("/api/recovery", {"token": token, "target": "127.0.0.1", "tier": "ADMIN",
                       "reason": "restore dashboard tier"})
st, body = call("/api/audit")
glass = [e for e in body["entries"] if e["action"] == "BREAK_GLASS"]
print(f"[{'PASS' if glass else 'FAIL'}] break-glass actions are in the audit chain ({len(glass)} entries)")

print("\n== state summary ==")
st, s = call("/api/state")
print("pairs cleared:", s["trading"]["agent"]["pairs_cleared"], "/", s["trading"]["agent"]["pairs_total"])
print("blocked ids  :", s["security"]["stats"]["blocked"], "| intrusions:", s["security"]["agent"]["intrusions"])
print("integrity    :", s["security"]["agent"]["integrity_ok"])

# The suite promotes demo intruders to DIREKTUR to prove the ladder; put the
# sandbox back the way it was so a human opening the dashboard does not find a
# simulated attacker holding the top tier.
call("/api/security/act", {"key": DIR, "target": "203.0.113.44", "tier": "BLOCKED",
                           "reason": "suite cleanup"}, cid="ops-console")
call("/api/security/act", {"key": DIR, "target": "198.51.100.7", "tier": "BIASA",
                           "reason": "suite cleanup"}, cid="ops-console")
print("cleanup      : 203.0.113.44 -> BLOCKED, 198.51.100.7 -> BIASA")
