"""HTTP + SSE server for 24/7 operation.

Hardening that matters when this is left running for months:

* every request passes through the security agent before routing;
* operator authority comes from an HttpOnly session cookie (no key in URLs, so
  no secret ends up in browser history or access logs);
* a background writer snapshots state to disk, and a heartbeat lets an external
  monitor tell "quiet" apart from "dead";
* SIGTERM/SIGINT flush state and close cleanly instead of being killed mid-write.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import signal
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.config import ROOT, cfg
from core.store import LOG_DIR, STORE
from security.agent import SecurityAgent
from trading.agent import TradingAgent

WEB_DIR = os.path.join(ROOT, "web")
VERSION = "1.2.0"
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".json": "application/json"}

SECURITY = SecurityAgent()
TRADING = TradingAgent(security=SECURITY)

SESSIONS: dict = {}
SESSION_TTL = 12 * 3600
STATE_SAVED_AT = [0.0]
SHUTDOWN = threading.Event()
BOOT_TS = time.time()

log = logging.getLogger("arena")


def setup_logging(verbose: bool = False) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not root.handlers:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(LOG_DIR, "arena.log"), maxBytes=5 * 1024 * 1024,
            backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(fh)
        root.addHandler(sh)


def save_state() -> None:
    try:
        STORE.save_state({"saved_at": time.time(), "version": VERSION,
                          "security": SECURITY.to_state(), "trading": TRADING.to_state()})
        STATE_SAVED_AT[0] = time.time()
    except Exception as exc:  # never let persistence kill the process
        log.warning("state snapshot failed: %s", exc)


def snapshot_loop() -> None:
    while not SHUTDOWN.wait(15):
        save_state()


def public_operator(op: dict | None) -> dict | None:
    """Never echo the operator key back over HTTP - tier and level are enough."""
    if not op:
        return None
    return {"tier": op.get("tier"), "level": op.get("level")}


def build_state(operator_key: str = "") -> dict:
    operator = public_operator(SECURITY.resolve_operator(operator_key)) if operator_key else None
    tier = operator.get("tier") if operator else None
    trading = TRADING.snapshot()
    security = SECURITY.snapshot()
    # BIASA redaction: no PnL, no positions, no strategies, no security internals
    if tier == "BIASA":
        trading["account"] = {"restricted": True}
        trading["positions"] = []
        trading["strategies"] = []
        trading["signals"] = []
        trading["trades"] = []
        trading["equity"] = []
        trading["pnl"] = []
        trading["mt5"] = None
        security = {"restricted": True, "agent": security.get("agent", {})}
    return {
        "ts": int(time.time() * 1000),
        "system": {"name": cfg("system.name", "SENTINEL ARENA"), "version": VERSION,
                   "bar_seconds": cfg("system.bar_seconds", 6),
                   "uptime_s": int(time.time() - TRADING.started_at)},
        "security": security,
        "trading": trading,
        "operator": operator,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SentinelArena/" + VERSION
    protocol_version = "HTTP/1.0"

    # ------------------------------------------------------------------ identity
    def _client_id(self, query: dict) -> str:
        cid = self.headers.get("X-Client-Id") or (query.get("cid", [""])[0] if query else "")
        return cid or self.client_address[0]

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _operator_key(self, query: dict) -> str:
        header = self.headers.get("X-Operator-Key") or ""
        if header and SECURITY.resolve_operator(header):
            return header
        sess = SESSIONS.get(self._cookie("arena_session"))
        if sess and time.time() - sess["created"] < SESSION_TTL:
            return sess["key"]
        return query.get("key", [""])[0] if query else ""

    def _session_operator(self) -> dict | None:
        """Who is behind this request, cookie first. None when not logged in."""
        sess = SESSIONS.get(self._cookie("arena_session"))
        if sess and time.time() - sess["created"] < SESSION_TTL:
            op = SECURITY.resolve_operator(sess["key"])
            if op:
                return op
        header = self.headers.get("X-Operator-Key") or ""
        if header:
            return SECURITY.resolve_operator(header)
        return None

    # ------------------------------------------------------------------ responses
    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'")

    def _json(self, payload: dict, status: int = 200, cookies: list | None = None) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    # ------------------------------------------------------------------ routing
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        decision = SECURITY.inspect(self._client_id(query), self.client_address[0], path,
                                    "GET", dict(self.headers), parsed.query)
        if not decision.allow:
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if wants_html and not path.startswith("/api/"):
                self._blocked_page(decision)
            else:
                self._json({"error": decision.reason, "tier": decision.tier,
                            "blocked": decision.blocked}, decision.status)
            return
        key = self._operator_key(query)
        try:
            if path in ("/", "/index.html", "/gate.html"):
                # opening view: the key + tier gate, unless a session already exists
                page = "index.html" if self._session_operator() else "gate.html"
                self._file(os.path.join(WEB_DIR, page))
            elif path == "/deck":
                if not self._session_operator():
                    self._redirect("/")
                else:
                    self._file(os.path.join(WEB_DIR, "index.html"))
            elif path in ("/app.js", "/style.css", "/gate.js", "/gate.css"):
                self._file(os.path.join(WEB_DIR, path.lstrip("/")))
            elif path == "/api/whoami":
                self._whoami()
            elif path == "/api/state":
                self._json(build_state(key))
            elif path == "/api/health":
                self._health()
            elif path == "/api/audit":
                self._json({"chain_ok": SECURITY.verify_chain(), "entries": SECURITY.audit[-120:][::-1],
                            "persisted_entries": STORE.count_audit_lines(),
                            "saved_at": int(STATE_SAVED_AT[0] * 1000)})
            elif path == "/api/stream":
                self._stream(key)
            elif path == "/api/appeal/list":
                self._json(SECURITY.list_appeals(key))
            elif path.startswith("/trap/"):
                self._json({"error": "blocked"}, 403)
            else:
                self._json({"error": "not found", "path": path}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log.exception("GET %s failed", path)
            self._json({"error": f"internal: {exc}"}, 500)

    def _health(self) -> None:
        age = time.time() - TRADING.last_cycle_ts if TRADING.last_cycle_ts else 999
        ok = age < 30 and not SHUTDOWN.is_set()
        mode, _note = TRADING.current_mode()
        self._json({
            "ok": ok, "version": VERSION, "mode": mode,
            "account_source": "MT5" if (TRADING.mt5 and TRADING.mt5.live) else "PAPER",
            "mt5": TRADING.mt5.snapshot() if TRADING.mt5 is not None else None,
            "uptime_s": int(time.time() - TRADING.started_at),
            "process_age_s": int(time.time() - BOOT_TS),
            "heartbeat_age_s": round(age, 1), "ticks": TRADING.ticks,
            "cycle_errors": TRADING.error_count,
            "paused": TRADING.paused, "killed": TRADING.killed,
            "halt_reason": TRADING.halt_reason,
            "pairs": {"total": len(TRADING.registry),
                      "armed": sum(1 for r in TRADING.registry.values()
                                   if r.status in ("SIM-VALIDATED", "LIVE-ELIGIBLE"))},
            "security": {"integrity_ok": SECURITY.integrity_ok and SECURITY.verify_chain(),
                         "blocked": sum(1 for e in SECURITY.entities.values() if e.tier == "BLOCKED"),
                         "intrusions": SECURITY.intrusions},
            "state_saved_age_s": round(time.time() - STATE_SAVED_AT[0], 1) if STATE_SAVED_AT[0] else None,
        }, 200 if ok else 503)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        body = self._body()
        decision = SECURITY.inspect(self._client_id(query), self.client_address[0], path,
                                    "POST", dict(self.headers), parsed.query, json.dumps(body))
        if not decision.allow and path != "/api/recovery":
            self._json({"error": decision.reason, "tier": decision.tier,
                        "blocked": decision.blocked}, decision.status)
            return
        key = body.get("key") or self._operator_key(query)
        try:
            if path == "/api/login":
                self._login(body)
            elif path == "/api/recovery":
                self._recovery(body)
            elif path == "/api/logout":
                token = self._cookie("arena_session")
                SESSIONS.pop(token, None)
                self._json({"ok": True}, 200,
                           ["arena_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"])
            elif path == "/api/security/act":
                result = SECURITY.set_tier(key, body.get("target", ""), body.get("tier", ""),
                                           body.get("reason", ""), body.get("code", ""))
                self._json(result, 200 if result.get("ok") else 403)
            elif path == "/api/security/attack":
                ok, msg = SECURITY.authorize(key, "ADMIN")
                if not ok:
                    self._json({"ok": False, "error": msg}, 403)
                    return
                self._json({"ok": True, "result": SECURITY.simulate_attack(body.get("kind", "canary"))})
            elif path == "/api/trading/control":
                ok, msg = SECURITY.authorize(key, "ADMIN")
                if not ok:
                    self._json({"ok": False, "error": msg}, 403)
                    return
                actor = (SECURITY.resolve_operator(key) or {}).get("tier", "ADMIN")
                self._json({"ok": True, "result": TRADING.control(body.get("action", "pause"), actor)})
            elif path == "/api/validate":
                ok, msg = SECURITY.authorize(key, "ADMIN")
                if not ok:
                    self._json({"ok": False, "error": msg}, 403)
                    return
                strat, sym = body.get("strategy", ""), body.get("symbol", "")
                if strat not in ("sr", "smc", "liq", "macro", "confluence"):
                    self._json({"ok": False, "error": "unknown strategy"}, 400)
                    return
                self._json({"ok": True, "result": TRADING.validate_pair(strat, sym).as_dict()})
            elif path == "/api/appeal":
                self._handle_appeal(body, key)
            elif path == "/api/appeal/list":
                self._json(SECURITY.list_appeals(key))
            elif path == "/api/appeal/approve":
                self._json(SECURITY.approve_appeal(key, body.get("id", ""), body.get("approve", True)))
            elif path == "/api/trading/lot":
                self._handle_lot(body, key)
            elif path == "/api/trading/timeframe":
                self._handle_timeframe(body, key)
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            log.exception("POST %s failed", path)
            self._json({"error": f"internal: {exc}"}, 500)

    def _recovery(self, body: dict) -> None:
        """Break-glass: requires the on-disk token, works even for a blocked IP."""
        token = str(body.get("token", ""))
        expected = STORE.read_recovery_key()
        if not expected or not hmac.compare_digest(token, expected):
            SECURITY.audit_event("ANON", "RECOVERY_DENIED", self.client_address[0],
                                 "break-glass token rejected")
            time.sleep(0.4)   # blunt any attempt to guess it
            self._json({"ok": False, "error": "invalid recovery token"}, 403)
            return
        result = SECURITY.set_tier_recovery(body.get("target", ""), body.get("tier", "BIASA"),
                                            body.get("reason", "operator locked out"),
                                            self.client_address[0])
        log.warning("break-glass: %s -> %s (%s)", body.get("target"), body.get("tier"), result.get("ok"))
        self._json(result, 200 if result.get("ok") else 400)

    def _blocked_page(self, decision) -> None:
        ip = self.client_address[0]
        reason = (decision.reason or "").replace("<", "&lt;")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        html = f"""<!DOCTYPE html><html lang="id"><head><meta charset="utf-8">
<title>SENTINEL ARENA - Akses Ditolak</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#05080e;color:#dbe7f3;font-family:ui-monospace,Consolas,monospace;
display:grid;place-items:center;min-height:100vh;padding:24px;
background-image:radial-gradient(600px 300px at 50% 0%,rgba(255,77,103,.10),transparent 70%)}}
.box{{max-width:640px;width:100%;border:1px solid rgba(255,77,103,.35);background:#0a111c;padding:28px}}
.tag{{color:#ff4d67;letter-spacing:.28em;font-size:11px}}
h1{{font-size:22px;letter-spacing:.12em;margin:10px 0 4px;color:#fff}}
.meta{{font-size:11px;color:#6f8298;margin:14px 0 18px;line-height:1.9;
border-left:2px solid rgba(255,77,103,.4);padding-left:12px}}
.meta b{{color:#ff8095;font-weight:500}}
h2{{font-size:11px;letter-spacing:.2em;color:#2ee6c8;margin:18px 0 8px}}
ol{{margin-left:18px;font-size:12px;line-height:1.9;color:#c9dcec}}
code{{background:rgba(46,230,200,.08);border:1px solid rgba(46,230,200,.2);
padding:2px 6px;color:#59f2da;font-size:11px}}
.note{{margin-top:18px;font-size:10.5px;color:#41536b;line-height:1.7}}
</style></head><body><div class="box">
<div class="tag">SENTINEL // ACCESS DENIED</div>
<h1>Identitas ini diblokir</h1>
<div class="meta">
  entitas&nbsp;&nbsp;: <b>{ip}</b><br>
  alasan&nbsp;&nbsp;&nbsp;: <b>{reason}</b><br>
  waktu&nbsp;&nbsp;&nbsp;&nbsp;: {stamp} UTC<br>
  status&nbsp;&nbsp;&nbsp;: BLOKIR (permanen sampai ada perintah eksplisit)
</div>
<h2>JALUR PEMULIHAN</h2>
<ol>
  <li>Di mesin yang menjalankan arena:<br><code>python tools/arena_admin.py unblock {ip}</code></li>
  <li>Atau dari IP lain, login dengan kunci DIREKTUR lalu pulihkan lewat dashboard.</li>
  <li>Atau minta ADMIN/DIREKTUR menjalankan pemulihan dari sesi mereka.</li>
</ol>
<div class="note">Blokir tidak kedaluwarsa sendiri. Ini disengaja: agent keamanan tidak
boleh membuka akses tanpa keputusan manusia. Pemulihan hanya bisa dilakukan lewat
akses filesystem ke host atau otoritas operator yang sah, dan setiap pemulihan
tercatat di audit chain.</div>
</div></body></html>"""
        body = html.encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _whoami(self) -> None:
        op = self._session_operator()
        ident = self._client_id({})
        ent = SECURITY.find_entity(ident) or SECURITY.find_entity(self.client_address[0])
        self._json({
            "ok": True, "operator": public_operator(op),
            "entity": ent.eid if ent else self.client_address[0],
            "tier": ent.tier if ent else "BIASA",
            "blocked": bool(ent and ent.tier == "BLOCKED"),
            "reason": ent.block_reason if ent else "",
        })

    def _login(self, body: dict) -> None:
        key = str(body.get("key", "")).strip()
        claim = str(body.get("tier", "") or "").strip().upper()
        op = SECURITY.resolve_operator(key)
        if not op:
            SECURITY.report_auth_failure(self._client_id({}))
            SECURITY.audit_event("ANON", "LOGIN_FAIL", self.client_address[0],
                                 f"unknown key prefix={key[:3] or '-'}")
            self._json({"ok": False, "error": "API key tidak dikenal"}, 401)
            return
        if claim and claim != op["tier"]:
            # a BIASA key may not claim to be DIREKTUR - the key decides, not the button
            SECURITY.audit_event("ANON", "LOGIN_TIER_MISMATCH", self.client_address[0],
                                 f"claimed={claim} actual={op['tier']}")
            self._json({"ok": False,
                        "error": f"Kunci ini terdaftar sebagai {op['tier']}, bukan {claim}"}, 403)
            return
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = {"key": key, "created": time.time()}
        for tok, sess in list(SESSIONS.items()):   # expire old sessions
            if time.time() - sess["created"] > SESSION_TTL:
                SESSIONS.pop(tok, None)
        SECURITY.audit_event(op["tier"], "LOGIN_OK", self.client_address[0],
                             f"session opened (tier={op['tier']})")
        log.info("session opened for %s from %s", op["tier"], self.client_address[0])
        self._json({"ok": True, "operator": public_operator(op)}, 200,
                   [f"arena_session={token}; Path=/; Max-Age={SESSION_TTL}; "
                    f"HttpOnly; SameSite=Strict"])

    def _handle_appeal(self, body: dict, key: str) -> None:
        eid = body.get("eid") or self._client_id({})
        target = str(body.get("target_tier", "ADMIN")).upper()
        reason = str(body.get("reason", ""))
        self._json(SECURITY.submit_appeal(eid, target, reason))

    def _handle_lot(self, body: dict, key: str) -> None:
        ok, msg = SECURITY.authorize(key, "DIREKTUR")
        if not ok:
            self._json({"ok": False, "error": msg}, 403)
            return
        klass = str(body.get("klass", "")).upper()
        lots = body.get("lots")
        try:
            lots = float(lots) if lots is not None else None
        except (TypeError, ValueError):
            self._json({"ok": False, "error": "Invalid lot value"}, 400)
            return
        result = TRADING.set_lot_override(klass, lots)
        self._json({"ok": True, "result": result})

    def _handle_timeframe(self, body: dict, key: str) -> None:
        ok, msg = SECURITY.authorize(key, "DIREKTUR")
        if not ok:
            self._json({"ok": False, "error": msg}, 403)
            return
        klass = str(body.get("klass", "")).upper()
        tf = str(body.get("timeframe", "")).upper()
        valid = ("M1", "M5", "M15", "H1", "H4")
        if tf not in valid:
            self._json({"ok": False, "error": f"Timeframe must be one of {valid}"}, 400)
            return
        result = TRADING.set_timeframe(klass, tf)
        self._json({"ok": True, "result": result})

    # ------------------------------------------------------------------ sse
    def _stream(self, key: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._common_headers()
        self.end_headers()
        self.close_connection = True
        last = 0.0
        try:
            while not SHUTDOWN.is_set():
                now = time.time()
                if now - last >= 1.0:
                    last = now
                    self.wfile.write(f"data: {json.dumps(build_state(key), default=str)}\n\n".encode())
                    self.wfile.flush()
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            # the browser closed the tab / reloaded: normal for an event stream,
            # never let it become a traceback in the rotating log
            return
        finally:
            self.close_connection = True

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("ARENA_VERBOSE"):
            log.debug("%s - %s", self.client_address[0], fmt % args)


def serve(argv=None) -> None:
    setup_logging(os.environ.get("ARENA_VERBOSE") == "1")
    ap = argparse.ArgumentParser(description="SENTINEL ARENA")
    ap.add_argument("--host", default=None, help="bind address (default from config.json)")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--public", action="store_true",
                    help="bind 0.0.0.0 and accept remote browsers")
    args = ap.parse_args(argv)

    host = args.host or ("0.0.0.0" if args.public else cfg("system.host", "127.0.0.1"))
    port = args.port or int(cfg("system.port", 8787))

    TRADING.start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    save_state()
    STORE.ensure_recovery_key()

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        # another instance (or another program) already owns the port: leave quietly
        log.error("cannot bind %s:%s (%s) - another instance is probably running", host, port, exc)
        print(f"  port {port} already in use - another SENTINEL ARENA instance is running")
        TRADING.stop()
        return
    httpd.daemon_threads = True

    def _stop(signum, frame):
        log.info("signal %s received - shutting down", signum)
        SHUTDOWN.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    log.info("SENTINEL ARENA %s listening on http://%s:%s (mode %s)", VERSION, host, port, TRADING.mode)
    mode, note = TRADING.current_mode()
    print(f"  SENTINEL ARENA {VERSION}  ->  http://{host}:{port}")
    print(f"  halaman masuk  : http://{host}:{port}/  (API key + pilih tier)")
    print(f"  execution mode : {mode} ({note})")
    if TRADING.mt5 is not None:
        print("  MT5 feed       : menunggu terminal - buka MetaTrader 5 & login, "
              "sistem menyambung sendiri")
    print("  break-glass    : data/recovery.key  (python tools/arena_admin.py --help)")
    if host == "0.0.0.0":
        print("  remote access  : ENABLED - every visitor is inspected and must log in with an operator key")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        SHUTDOWN.set()
        TRADING.stop()
        save_state()
        httpd.server_close()
        STORE.release_writer()
        log.info("state flushed, server closed")


if __name__ == "__main__":
    serve()
