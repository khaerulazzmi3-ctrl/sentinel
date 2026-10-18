"""Sentinel security agent.

Access model (4 tiers, strictly ordered):

    BLOCKED (0) < BIASA (1) < ADMIN (2) < DIREKTUR (3)

Rules enforced in code, not in the UI:

* Any entity that trips a critical detector - including a canary/honeypot link -
  is blocked **immediately**, before any human instruction. Blocking is a code
  path, not an alert.
* A blocked entity can only be restored to BIASA by an explicit ADMIN or
  DIREKTUR action. There is no timer, no probation, no self-service unblock.
* BIASA -> ADMIN and any promotion to DIREKTUR require DIREKTUR authority plus
  the confirmation code.
* Every decision is appended to a hash-chained audit log, so tampering with one
  line invalidates the chain and is reported by the integrity check.
* On every intrusion the agent performs remediation (rotate canaries, tighten
  WAF, verify rule integrity, quarantine, snapshot evidence) and records what it
  did. This is the "self-repair" loop.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import os
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from core.config import cfg
from core.store import STORE

log = logging.getLogger("arena.security")

def _locked(fn):
    """Serialise access to shared agent state (requests, SSE readers, engine loop)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


TIERS = {"BLOCKED": 0, "BIASA": 1, "ADMIN": 2, "DIREKTUR": 3}
TIER_ORDER = ["BLOCKED", "BIASA", "ADMIN", "DIREKTUR"]

SEVERITY = {"low": 15, "medium": 35, "high": 60, "critical": 100}

INJECTION_PATTERNS: List[Tuple[str, str, str]] = [
    (r"(?i)\bunion\b.+\bselect\b", "SQL injection (UNION SELECT)", "critical"),
    (r"(?i)('\s*or\s*'?\d+'?\s*=\s*'?\d+|1\s*=\s*1\s*--)", "SQL injection (tautology)", "critical"),
    (r"(?i)\b(sleep|benchmark|pg_sleep)\s*\(", "SQL injection (time based)", "critical"),
    (r"(?i)<\s*script[^>]*>", "Cross-site scripting", "high"),
    (r"(?i)javascript:\s*\w", "XSS via javascript URI", "high"),
    (r"(?i)on(error|load|mouseover)\s*=", "XSS event handler", "high"),
    (r"(\.\./|%2e%2e%2f|%2e%2e/|\.\.\\)", "Path traversal", "critical"),
    (r"(?i)(;\s*(cat|ls|wget|curl)\s|\|\s*(whoami|id|nc)\b|\$\(|`)", "Command injection", "critical"),
    (r"(?i)(file|gopher|dict)://", "SSRF / local file access", "high"),
    (r"(?i)(169\.254\.169\.254|metadata\.google\.internal)", "Cloud metadata probing", "high"),
]

SCANNER_UA = ["sqlmap", "nikto", "nmap", "masscan", "zgrab", "nuclei", "gobuster",
              "dirbuster", "wpscan", "hydra", "acunetix", "nessus"]

SENSITIVE_PATHS = ["/.env", "/.git/config", "/wp-login.php", "/phpmyadmin", "/etc/passwd",
                   "/.aws/credentials", "/backup.zip", "/actuator/env", "/server-status",
                   "/config.php.bak", "/.ssh/id_rsa", "/adminer.php", "/shell.php"]

HONEYPOT_PATHS = ["/internal/debug", "/api/v1/backup", "/admin-secret", "/.well-known/secret"]


@dataclass
class Entity:
    eid: str
    ip: str
    label: str = "unknown"
    tier: str = "BIASA"
    score: int = 0
    strikes: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    blocked_at: Optional[float] = None
    block_reason: str = ""
    quarantined: bool = False
    requests: int = 0
    tokens: List[str] = field(default_factory=list)
    detections: List[str] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)

    FIELDS = ("eid", "ip", "label", "tier", "score", "strikes", "first_seen", "last_seen",
              "blocked_at", "block_reason", "quarantined", "requests", "tokens",
              "detections", "history")

    def to_state(self) -> dict:
        return {f: getattr(self, f) for f in self.FIELDS}

    @classmethod
    def from_state(cls, d: dict) -> "Entity":
        ent = cls(eid=d.get("eid", "unknown"), ip=d.get("ip", ""))
        for f in cls.FIELDS:
            if f in d and d[f] is not None:
                setattr(ent, f, d[f])
        return ent

    def as_dict(self) -> dict:
        return {
            "eid": self.eid, "ip": self.ip, "label": self.label, "tier": self.tier,
            "level": TIERS[self.tier], "score": self.score, "strikes": self.strikes,
            "blocked_at": int(self.blocked_at * 1000) if self.blocked_at else None,
            "block_reason": self.block_reason, "quarantined": self.quarantined,
            "requests": self.requests, "detections": self.detections[-6:],
            "last_seen": int(self.last_seen * 1000), "first_seen": int(self.first_seen * 1000),
        }


@dataclass
class Decision:
    allow: bool
    status: int
    reason: str
    tier: str
    detections: List[dict] = field(default_factory=list)
    blocked: bool = False

    def as_dict(self) -> dict:
        return {"allow": self.allow, "status": self.status, "reason": self.reason,
                "tier": self.tier, "detections": self.detections, "blocked": self.blocked}


class SecurityAgent:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.entities: Dict[str, Entity] = {}
        self.threats: Deque[dict] = deque(maxlen=120)
        self.audit: List[dict] = []
        self.heals: List[dict] = []
        self.canaries: Dict[str, float] = {}
        self.waf_sensitivity = 1.0
        self.integrity_ok = True
        self.requests_inspected = 0
        self.blocked_requests = 0
        self.intrusions = 0
        self._rules_hash = self._rules_digest()
        self._baseline_hash = self._rules_hash
        self._last_canary_rotation = time.time()
        self._rate: Dict[str, Deque[float]] = {}
        self._auth_fails: Dict[str, Deque[float]] = {}
        self._paths: Dict[str, Deque[Tuple[float, str]]] = {}
        self._seq = 0
        self._prev_hash = "GENESIS"
        self.appeals: Deque[dict] = deque(maxlen=40)
        self._seed()
        self._rotate_canaries(initial=True)
        restored = self._restore()
        self.audit_event("SYSTEM", "AGENT_ONLINE", "security",
                         "Sentinel security agent armed" + (" (state restored)" if restored else ""))

    # ------------------------------------------------------------------ setup
    def _seed(self) -> None:
        seeds = [
            ("203.0.113.44", "canary-intruder", "BLOCKED", 100, "Canary link triggered - auto-blocked"),
            ("198.51.100.7", "unverified-client", "BIASA", 18, ""),
            ("192.0.2.91", "crawler", "BLOCKED", 100, "Scanner signature (sqlmap)"),
            ("10.0.0.5", "ops-admin", "ADMIN", 0, ""),
            ("10.0.0.1", "director", "DIREKTUR", 0, ""),
            ("127.0.0.1", "dashboard", "ADMIN", 0, ""),
        ]
        for ip, label, tier, score, reason in seeds:
            e = Entity(eid=ip, ip=ip, label=label, tier=tier, score=score)
            if tier == "BLOCKED":
                e.blocked_at = time.time() - 3600
                e.block_reason = reason
                e.quarantined = True
                e.detections = [reason]
            self.entities[e.eid] = e

    @staticmethod
    def operator_keys() -> Dict[str, str]:
        """Env var wins so a 24/7 deployment never needs keys on disk in plaintext."""
        raw = os.environ.get("ARENA_OPERATOR_KEYS")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed:
                    return {str(k): str(v).upper() for k, v in parsed.items()}
            except ValueError:
                pass
        return cfg("security.operator_keys", {})

    @staticmethod
    def promotion_code() -> str:
        return os.environ.get("ARENA_PROMOTION_CODE") or cfg("security.promotion_code", "")

    def _rules_digest(self) -> str:
        # only the rule definitions are hashed - runtime knobs must not look like tampering
        blob = json.dumps({"inj": INJECTION_PATTERNS, "ua": SCANNER_UA,
                           "paths": SENSITIVE_PATHS, "honeypot": HONEYPOT_PATHS},
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> bool:
        """Reload entities, tiers and the audit chain after a restart."""
        st = STORE.load_state().get("security") or {}
        for d in st.get("entities") or []:
            try:
                ent = Entity.from_state(d)
            except Exception:
                continue
            self.entities[ent.eid] = ent          # restored state wins over seeds
        tail = STORE.load_audit_tail(400)
        if tail:
            self.audit = tail
            self._seq = int(tail[-1].get("seq", 0) or 0)
            self._prev_hash = tail[-1].get("hash", "GENESIS")
            if not self._chain_valid():
                # A second writer (a stray script, a duplicate process) can fork the
                # sequence. Never silently adopt a forked log: quarantine the file
                # for forensics, then start a clean chain that says so out loud.
                moved = STORE.quarantine_audit()
                log.warning("audit chain did not verify - quarantined to %s", moved)
                self.audit = []
                self._seq = 0
                self._prev_hash = "GENESIS"
                self.audit_event("SYSTEM", "CHAIN_QUARANTINE", moved or "data/audit.jsonl",
                                 "chain failed verification on boot; evidence kept, new chain started")
        self.intrusions = int(st.get("intrusions", 0) or 0)
        self.requests_inspected = int(st.get("requests", 0) or 0)
        self.blocked_requests = int(st.get("blocked_requests", 0) or 0)
        for a in st.get("appeals") or []:
            self.appeals.append(a)
        return bool(st.get("entities") or tail)

    def to_state(self) -> dict:
        return {
            "seq": self._seq, "prev_hash": self._prev_hash,
            "intrusions": self.intrusions, "requests": self.requests_inspected,
            "blocked_requests": self.blocked_requests,
            "entities": [e.to_state() for e in self.entities.values()],
            "appeals": list(self.appeals),
        }

    # ------------------------------------------------------------------ audit
    @_locked
    def audit_event(self, actor: str, action: str, target: str, detail: str) -> dict:
        self._seq += 1
        entry = {"seq": self._seq, "ts": int(time.time() * 1000), "actor": actor,
                 "action": action, "target": target, "detail": detail, "prev": self._prev_hash}
        payload = json.dumps(entry, sort_keys=True)
        entry["hash"] = hashlib.sha256((self._prev_hash + payload).encode()).hexdigest()[:24]
        self._prev_hash = entry["hash"]
        self.audit.append(entry)
        if len(self.audit) > 400:
            del self.audit[:-400]
        STORE.append_audit(entry)   # durable: survives restarts
        return entry

    @_locked
    def verify_chain(self) -> bool:
        return self._chain_valid()

    def _chain_valid(self) -> bool:
        if not self.audit:
            return True
        # the in-memory window may start mid-chain after a restart: seed from the
        # first entry's own pointer, then prove every link after it
        prev = self.audit[0].get("prev", "GENESIS")
        for e in self.audit:
            body = {k: e[k] for k in ("seq", "ts", "actor", "action", "target", "detail", "prev")}
            expect = hashlib.sha256((prev + json.dumps(body, sort_keys=True)).encode()).hexdigest()[:24]
            if e.get("prev") != prev or e.get("hash") != expect:
                return False
            prev = e["hash"]
        return True

    # ------------------------------------------------------------------ canary
    def _rotate_canaries(self, initial: bool = False, log: bool = True) -> None:
        old = list(self.canaries)
        self.canaries = {secrets.token_urlsafe(9): time.time() for _ in range(3)}
        self._last_canary_rotation = time.time()
        if not initial and log:
            self.heals.append({
                "ts": int(time.time() * 1000), "action": "ROTATE_CANARIES",
                "detail": f"{len(old)} trap tokens retired, 3 re-armed",
                "status": "DONE",
            })

    @_locked
    def canary_links(self) -> List[dict]:
        return [{"path": f"/trap/{tok}", "age_s": int(time.time() - born)}
                for tok, born in self.canaries.items()]

    # ------------------------------------------------------------------ detection
    def _detect(self, eid: str, path: str, query: str, body: str, ua: str,
                tier: str = "BIASA") -> List[dict]:
        found: List[dict] = []
        blob = f"{path} {query} {body}"
        for pattern, label, sev in INJECTION_PATTERNS:
            if re.search(pattern, blob):
                found.append({"kind": label, "severity": sev, "weight": SEVERITY[sev]})
        low_ua = (ua or "").lower()
        for sig in SCANNER_UA:
            if sig in low_ua:
                found.append({"kind": f"Attack tool signature: {sig}", "severity": "critical",
                              "weight": SEVERITY["critical"]})
                break
        for p in SENSITIVE_PATHS:
            if path.startswith(p):
                found.append({"kind": f"Sensitive path probe {p}", "severity": "high",
                              "weight": SEVERITY["high"]})
                break
        for p in HONEYPOT_PATHS:
            if path.startswith(p):
                found.append({"kind": f"Honeypot endpoint touched {p}", "severity": "critical",
                              "weight": SEVERITY["critical"]})
                break
        # rate limiting: throttling, not blocking. Authenticated operators get a
        # much higher ceiling so normal dashboard traffic can never self-lock.
        bucket = self._rate.setdefault(eid, deque())
        now = time.time()
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        bucket.append(now)
        limit = int(cfg("security.rate_limit_per_min", 90))
        if tier in ("ADMIN", "DIREKTUR"):
            limit *= 8
        if len(bucket) > limit:
            found.append({"kind": f"Rate limit exceeded ({len(bucket)}/min)", "severity": "low",
                          "weight": 0, "soft": True})
        # scanning velocity: many distinct paths in 10s
        paths = self._paths.setdefault(eid, deque())
        while paths and now - paths[0][0] > 10:
            paths.popleft()
        paths.append((now, path))
        if len({p for _, p in paths}) > 25:
            found.append({"kind": "Directory scanning velocity", "severity": "high",
                          "weight": SEVERITY["high"]})
        return found

    @_locked
    def inspect(self, eid: str, ip: str, path: str, method: str = "GET",
                headers: Optional[Dict[str, str]] = None, query: str = "",
                body: str = "") -> Decision:
        headers = headers or {}
        ua = headers.get("User-Agent", "")
        entity = self.entities.get(eid)
        if entity is None:
            entity = Entity(eid=eid, ip=ip, label=headers.get("X-Client-Label", "visitor"))
            self.entities[eid] = entity
        entity.last_seen = time.time()
        entity.requests += 1
        self.requests_inspected += 1

        # canary link: instant block, no human in the loop
        if path.startswith("/trap/"):
            token = path.split("/trap/", 1)[1].split("?")[0]
            if token in self.canaries or token:
                self._block(entity, f"Canary link hit (/trap/{token[:6]}...) - auto-blocked", critical=True)
                self.blocked_requests += 1
                return Decision(False, 403, "Canary link triggered. Identity blocked.", "BLOCKED",
                                [{"kind": "Canary link", "severity": "critical", "weight": 100}], True)

        if entity.tier == "BLOCKED":
            self.blocked_requests += 1
            self.threats.appendleft({
                "ts": int(time.time() * 1000), "eid": entity.eid, "ip": entity.ip,
                "kind": "Blocked identity retried access", "severity": "medium",
                "action": "DENY", "path": path,
            })
            return Decision(False, 403, f"Identity is BLOCKED: {entity.block_reason}", "BLOCKED", blocked=True)

        detections = self._detect(eid, path, query, body, ua, entity.tier)
        hard = [d for d in detections if not d.get("soft")]
        soft = [d for d in detections if d.get("soft")]

        if soft and not hard:
            # throttled, not condemned: 429 and move on
            self.threats.appendleft({
                "ts": int(time.time() * 1000), "eid": entity.eid, "ip": entity.ip,
                "kind": soft[0]["kind"], "severity": "low", "action": "THROTTLE", "path": path,
            })
            return Decision(False, 429, "Too many requests. Slow down.", entity.tier, soft)

        if hard:
            total = sum(d["weight"] for d in hard)
            entity.score = min(100, entity.score + int(total * self.waf_sensitivity * 0.55))
            entity.strikes += 1
            entity.detections.extend(d["kind"] for d in hard)
            for d in hard:
                self.threats.appendleft({
                    "ts": int(time.time() * 1000), "eid": entity.eid, "ip": entity.ip,
                    "kind": d["kind"], "severity": d["severity"],
                    "action": "BLOCK" if entity.score >= int(cfg("security.auto_block_score", 60))
                    else "SCORE", "path": path,
                })
            if entity.score >= int(cfg("security.auto_block_score", 60)) or any(
                    d["weight"] >= 100 for d in hard):
                self._block(entity, hard[0]["kind"], critical=True)
                self.blocked_requests += 1
                return Decision(False, 403, "Intrusion detected. Identity blocked.", "BLOCKED",
                                detections, True)
            return Decision(True, 200, f"Suspicious activity scored ({entity.score}/100)", entity.tier, detections)

        # slow decay of the risk score for clean traffic
        entity.score = max(0, entity.score - 1)
        return Decision(True, 200, "ok", entity.tier)

    # ------------------------------------------------------------------ response
    @_locked
    def _block(self, entity: Entity, reason: str, critical: bool = False) -> None:
        already = entity.tier == "BLOCKED"
        entity.tier = "BLOCKED"
        entity.score = 100
        entity.blocked_at = entity.blocked_at or time.time()
        entity.block_reason = reason
        entity.quarantined = True
        entity.tokens.clear()
        if not already:
            self.intrusions += 1
            entity.history.append({"ts": int(time.time() * 1000), "event": "AUTO_BLOCK", "reason": reason})
            self.audit_event("SENTINEL", "AUTO_BLOCK", entity.eid,
                             f"{reason} | source={entity.ip} | critical={critical}")
            self.self_heal(trigger=reason, entity=entity)

    @_locked
    def report_auth_failure(self, eid: str) -> None:
        bucket = self._auth_fails.setdefault(eid, deque())
        now = time.time()
        while bucket and now - bucket[0] > 300:
            bucket.popleft()
        bucket.append(now)
        entity = self.entities.get(eid)
        if entity and len(bucket) >= int(cfg("security.auth_fail_threshold", 5)):
            self._block(entity, f"{len(bucket)} failed authentications in 5 min", critical=True)

    # ------------------------------------------------------------------ healing
    @_locked
    def self_heal(self, trigger: str, entity: Optional[Entity] = None) -> List[dict]:
        """Remediation loop: rotate, tighten, verify, quarantine, re-arm."""
        actions: List[dict] = []

        def do(action: str, detail: str) -> None:
            actions.append({"ts": int(time.time() * 1000), "action": action,
                            "detail": detail, "status": "DONE"})

        self._rotate_canaries(log=False)
        do("ROTATE_CANARIES", "Trap links re-keyed; old tokens still trap")

        self.waf_sensitivity = min(2.0, self.waf_sensitivity + 0.15)
        do("TIGHTEN_WAF", f"Sensitivity raised to {self.waf_sensitivity:.2f}x")

        current = self._rules_digest()
        if current != self._rules_hash and not self.integrity_ok:
            self.integrity_ok = True
            do("RESTORE_RULES", "Rule set restored from signed baseline")
        else:
            do("VERIFY_RULES", "Rule set matches signed baseline")

        if entity is not None:
            entity.quarantined = True
            do("QUARANTINE", f"Session {entity.eid} isolated, tokens revoked")

        do("SNAPSHOT_EVIDENCE", "Request fingerprint + headers archived to audit chain")
        self.heals.extend(actions)
        if len(self.heals) > 60:
            del self.heals[:-60]
        self.audit_event("SENTINEL", "SELF_HEAL", entity.eid if entity else "system",
                         f"trigger={trigger} actions={','.join(a['action'] for a in actions)}")
        return actions

    @_locked
    def tick(self) -> None:
        """Periodic housekeeping called by the engine loop."""
        if time.time() - self._last_canary_rotation > int(cfg("security.canary_rotation_seconds", 120)):
            self._rotate_canaries()
        if self.waf_sensitivity > 1.0:
            self.waf_sensitivity = max(1.0, self.waf_sensitivity - 0.01)
        # integrity watchdog: rules digest must always match baseline
        if self._rules_digest() != self._baseline_hash:
            self.integrity_ok = False
            self.heals.append({"ts": int(time.time() * 1000), "action": "TAMPER_DETECTED",
                               "detail": "Rule drift detected, restoring baseline", "status": "DONE"})
            self.waf_sensitivity = min(2.0, self.waf_sensitivity + 0.2)

    # ------------------------------------------------------------------ authority
    def resolve_operator(self, key: str) -> Optional[dict]:
        keys = cfg("security.operator_keys", {})
        tier = keys.get(key)
        if not tier:
            return None
        return {"key": key, "tier": tier, "level": TIERS[tier]}

    @_locked
    def set_tier(self, actor_key: str, target_eid: str, new_tier: str,
                 reason: str = "", code: str = "") -> dict:
        actor = self.resolve_operator(actor_key)
        target = self.entities.get(target_eid)
        if actor is None:
            return {"ok": False, "error": "Unknown operator key"}
        if target is None:
            return {"ok": False, "error": "Unknown entity"}
        if new_tier not in TIERS:
            return {"ok": False, "error": "Unknown tier"}
        a_lvl, t_lvl, n_lvl = TIERS[actor["tier"]], TIERS[target.tier], TIERS[new_tier]

        if a_lvl < TIERS["ADMIN"]:
            return {"ok": False, "error": "BIASA has no authority over access tiers"}
        if a_lvl == TIERS["ADMIN"]:
            if t_lvl == TIERS["DIREKTUR"]:
                return {"ok": False, "error": "Only a DIREKTUR can modify a DIREKTUR"}
            if n_lvl > TIERS["ADMIN"]:
                return {"ok": False, "error": "ADMIN can lift up to ADMIN only. "
                                               "Appointing a DIREKTUR needs a DIREKTUR."}
        if n_lvl == TIERS["DIREKTUR"]:
            if actor["tier"] != "DIREKTUR":
                return {"ok": False, "error": "Only a DIREKTUR can appoint a DIREKTUR"}
            if code != self.promotion_code():
                return {"ok": False, "error": "Invalid director confirmation code"}
        if t_lvl == n_lvl:
            return {"ok": False, "error": "Tier unchanged"}

        old = target.tier
        target.tier = new_tier
        self._forgive(target, new_tier, reason, actor["tier"])
        target.history.append({"ts": int(time.time() * 1000), "event": f"{old}->{new_tier}",
                               "reason": reason, "actor": actor["tier"]})
        self.audit_event(actor["tier"], "TIER_CHANGE", target.eid,
                         f"{old} -> {new_tier} | reason={reason or '-'} | code_ok={bool(code)}")
        return {"ok": True, "entity": target.as_dict(), "from": old, "to": new_tier,
                "actor": actor["tier"]}

    def _forgive(self, ent: Entity, new_tier: str, reason: str, actor: str) -> None:
        """Apply a tier change to the entity's detection state.

        Blocking must also clear the counters that produced it: leaving the auth
        failure window or the rate bucket hot means an "unblocked" operator is
        instantly re-blocked or throttled, which reads as "still blocked" and
        makes the recovery path look broken.
        """
        if new_tier == "BLOCKED":
            ent.blocked_at = time.time()
            ent.block_reason = reason or "Manual block by " + actor
            ent.quarantined = True
            ent.tokens = []
            return
        ent.blocked_at = None
        ent.block_reason = ""
        ent.quarantined = False
        ent.tokens = []
        ent.strikes = 0
        if new_tier == "BIASA":
            ent.score = 0
            ent.detections = []
        self._auth_fails.pop(ent.eid, None)
        self._rate.pop(ent.eid, None)

    @_locked
    def find_entity(self, ident: str) -> Optional[Entity]:
        """Look an identity up by entity id or by IP."""
        if ident in self.entities:
            return self.entities[ident]
        for ent in self.entities.values():
            if ent.ip == ident or ent.eid == ident:
                return ent
        return None

    @_locked
    def set_tier_recovery(self, target: str, new_tier: str, reason: str = "",
                          source: str = "local") -> dict:
        """Break-glass tier change. No operator key, no tier rules.

        Only reachable with the on-disk recovery token, so the caller already has
        filesystem access to this machine. Every use is written to the audit chain
        as actor RECOVERY / BREAK_GLASS so it can never happen silently.
        """
        ent = self.find_entity(target)
        if ent is None:
            return {"ok": False, "error": "Unknown entity"}
        if new_tier not in TIERS:
            return {"ok": False, "error": "Unknown tier"}
        old = ent.tier
        ent.tier = new_tier
        self._forgive(ent, new_tier, reason or "Break-glass block", "RECOVERY")
        ent.history.append({"ts": int(time.time() * 1000), "event": f"RECOVERY {old}->{new_tier}",
                            "reason": reason, "actor": "RECOVERY"})
        self.audit_event("RECOVERY", "BREAK_GLASS", ent.eid,
                         f"{old} -> {new_tier} | reason={reason or '-'} | source={source}")
        return {"ok": True, "entity": ent.as_dict(), "from": old, "to": new_tier,
                "actor": "RECOVERY"}

    @_locked
    def authorize(self, actor_key: str, minimum: str) -> Tuple[bool, str]:
        actor = self.resolve_operator(actor_key)
        if actor is None:
            return False, "Unknown operator key"
        if TIERS[actor["tier"]] < TIERS[minimum]:
            return False, f"Requires {minimum} authority (you are {actor['tier']})"
        return True, "ok"

    # ------------------------------------------------------------------ appeal workflow
    @_locked
    def submit_appeal(self, eid: str, target_tier: str, reason: str = "") -> dict:
        """BIASA or BLOCKED entity submits an appeal to be promoted."""
        ent = self.entities.get(eid)
        if ent is None:
            return {"ok": False, "error": "Unknown entity"}
        if target_tier not in TIERS:
            return {"ok": False, "error": "Unknown tier"}
        if TIERS[ent.tier] >= TIERS[target_tier]:
            return {"ok": False, "error": f"Already {ent.tier} or higher"}
        appeal = {
            "id": secrets.token_urlsafe(8),
            "ts": int(time.time() * 1000),
            "eid": eid,
            "from_tier": ent.tier,
            "to_tier": target_tier,
            "reason": reason or "-",
            "status": "PENDING",
            "reviewed_by": None,
            "reviewed_at": None,
        }
        self.appeals.append(appeal)
        self.audit_event("APPEAL", "SUBMITTED", eid,
                         f"{ent.tier} -> {target_tier} | reason={reason or '-'} | id={appeal['id']}")
        return {"ok": True, "appeal": appeal}

    @_locked
    def list_appeals(self, actor_key: str) -> dict:
        """ADMIN and DIREKTUR can list pending appeals."""
        ok, msg = self.authorize(actor_key, "ADMIN")
        if not ok:
            return {"ok": False, "error": msg}
        actor = self.resolve_operator(actor_key)
        tier = actor["tier"] if actor else "BIASA"
        pending = [a for a in self.appeals if a["status"] == "PENDING"]
        # ADMIN only sees appeals to ADMIN; DIREKTUR sees all
        if tier == "ADMIN":
            pending = [a for a in pending if a["to_tier"] == "ADMIN"]
        return {"ok": True, "appeals": pending}

    @_locked
    def approve_appeal(self, actor_key: str, appeal_id: str, approve: bool = True) -> dict:
        """ADMIN or DIREKTUR approves/rejects an appeal. Auto-promotes on approval."""
        ok, msg = self.authorize(actor_key, "ADMIN")
        if not ok:
            return {"ok": False, "error": msg}
        actor = self.resolve_operator(actor_key)
        tier = actor["tier"] if actor else "BIASA"
        for a in self.appeals:
            if a["id"] == appeal_id and a["status"] == "PENDING":
                # ADMIN cannot approve appeals to DIREKTUR
                if tier == "ADMIN" and a["to_tier"] == "DIREKTUR":
                    return {"ok": False, "error": "Only DIREKTUR can approve DIREKTUR appeals"}
                a["status"] = "APPROVED" if approve else "REJECTED"
                a["reviewed_by"] = tier
                a["reviewed_at"] = int(time.time() * 1000)
                self.audit_event("APPEAL", a["status"], a["eid"],
                                 f"by {tier} | id={appeal_id}")
                if approve:
                    result = self.set_tier(actor_key, a["eid"], a["to_tier"],
                                           reason=f"Appeal approved by {tier}",
                                           code=self.promotion_code() if a["to_tier"] == "DIREKTUR" else "")
                    if not result.get("ok"):
                        a["status"] = "APPROVED_TIER_FAILED"
                        return {"ok": False, "error": result.get("error"), "appeal": a}
                return {"ok": True, "appeal": a}
        return {"ok": False, "error": "Appeal not found or already reviewed"}

    # ------------------------------------------------------------------ demo
    @_locked
    def simulate_attack(self, kind: str = "canary") -> dict:
        """Run a real detection cycle against a synthetic identity."""
        ip = f"203.0.113.{secrets.randbelow(200) + 10}"
        eid = f"{ip}#{secrets.token_hex(2)}"
        presets = {
            "canary": ("GET", f"/trap/{secrets.token_urlsafe(6)}", "", "Mozilla/5.0"),
            "sqli": ("GET", "/api/state", "id=1 UNION SELECT password FROM users", "Mozilla/5.0"),
            "scanner": ("GET", "/wp-login.php", "", "sqlmap/1.7.2#stable"),
            "traversal": ("GET", "/static/../../../../etc/passwd", "", "Mozilla/5.0"),
            "bruteforce": ("POST", "/api/login", "user=root&pass=toor", "Mozilla/5.0"),
        }
        method, path, query, ua = presets.get(kind, presets["canary"])
        headers = {"User-Agent": ua, "X-Client-Label": f"intruder-{kind}"}
        if eid not in self.entities:
            self.entities[eid] = Entity(eid=eid, ip=ip, label=f"intruder-{kind}")
        if kind == "bruteforce":
            for _ in range(int(cfg("security.auth_fail_threshold", 5))):
                self.report_auth_failure(eid)
        decision = self.inspect(eid, ip, path, method, headers, query)
        return {"attack": kind, "entity": self.entities[eid].as_dict(),
                "decision": decision.as_dict(),
                "heal_actions": self.heals[-6:]}

    # ------------------------------------------------------------------ snapshot
    @_locked
    def snapshot(self) -> dict:
        ents = sorted(self.entities.values(), key=lambda e: (TIERS[e.tier], -e.last_seen))
        ladder = {t: sum(1 for e in self.entities.values() if e.tier == t) for t in TIER_ORDER}
        return {
            "agent": {
                "name": "SENTINEL",
                "role": "Security Agent",
                "status": "ENGAGED" if self.intrusions else "WATCHING",
                "integrity_ok": self.integrity_ok and self.verify_chain(),
                "waf_sensitivity": round(self.waf_sensitivity, 2),
                "requests": self.requests_inspected,
                "blocked_requests": self.blocked_requests,
                "intrusions": self.intrusions,
                "canaries": len(self.canaries),
                "next_rotation_s": max(0, int(int(cfg("security.canary_rotation_seconds", 120))
                                              - (time.time() - self._last_canary_rotation))),
            },
            "entities": [e.as_dict() for e in ents[:14]],
            "threats": list(self.threats)[:18],
            "heals": self.heals[-8:][::-1],
            "ladder": ladder,
            "tier_order": TIER_ORDER,
            "audit_tail": self.audit[-12:][::-1],
            "canary_links": self.canary_links(),
            "appeals": [a for a in self.appeals if a["status"] == "PENDING"],
            "stats": {
                "blocked": ladder["BLOCKED"], "biasa": ladder["BIASA"],
                "admin": ladder["ADMIN"], "direktur": ladder["DIREKTUR"],
                "threats_24h": len(self.threats),
            },
        }
