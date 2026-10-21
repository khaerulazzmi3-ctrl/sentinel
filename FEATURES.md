# SENTINEL ARENA — Dokumentasi Fitur & Strategy

## Daftar Isi
1. [Arsitektur Sistem](#1-arsitektur-sistem)
2. [Security Agent (4 Tier)](#2-security-agent-4-tier)
3. [Trading Agent](#3-trading-agent)
4. [Strategi Trading](#4-strategi-trading)
5. [Gate / Simulasi](#5-gate--simulasi)
6. [MetaTrader 5 Integration](#6-metatrader-5-integration)
7. [Dashboard Web](#7-dashboard-web)
8. [Risk Management](#8-risk-management)
9. [API Endpoints](#9-api-endpoints)
10. [Deployment](#10-deployment)

---

## 1. Arsitektur Sistem

```
                    ┌──────────────────────────────┐
   HP/Tablet/Laptop ──────►│  HTTP + SSE (server/app.py)  │
                    │      Port 8787, host 0.0.0.0       │
                    └──────────┬───────────────────┘
                               │ setiap request diperiksa dulu
                    ┌──────────▼───────────────────┐
                    │  SENTINEL (security agent)   │
                    │  • Auto-block intruders      │
                    │  • Self-healing              │
                    │  • Hash-chain audit          │
                    │  • 4-tier access control     │
                    └──────────┬───────────────────┘
                               │ tier check
                    ┌──────────▼───────────────────┐
                    │  ATLAS (trading agent)       │
                    │  • Gate validation           │
                    │  • Risk management           │
                    │  • Auto-execution            │
                    └──────────┬───────────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     ┌────────▼──────┐  ┌─────▼──────┐  ┌──────▼──────┐
     │ MT5 Broker    │  │ Paper      │  │ Router      │
     │ (order nyata) │  │ Broker     │  │ Broker      │
     └───────────────┘  │ (simulasi) │  │ (auto-switch│
                        └────────────┘  └─────────────┘
```

---

## 2. Security Agent (4 Tier)

### Model Akses

```
BLOCKED (0)  <  BIASA (1)  <  ADMIN (2)  <  DIREKTUR (3)
```

### Detail Setiap Tier

| Tier | Hak Akses | Bisa Melihat | Bisa Lakukan |
|------|-----------|-------------|-------------|
| **BLOCKED** | Tidak ada | Tidak bisa akses | Tidak bisa apa-apa. Auto-blocked oleh agent. |
| **BIASA** | Terbatas | Status sistem, appeal form | Ajukan banding untuk naik ke ADMIN/DIREKTUR |
| **ADMIN** | Hampir penuh | Semua data, security ladder | Pause/resume trading, simulasi attack, unblock, promote BIASA→ADMIN |
| **DIREKTUR** | Penuh | Semua data + kontrol | Semua hak ADMIN + set lot/timeframe, promote ke DIREKTUR |

### Auto-Block Rules

Agent security memblokir otomatis tanpa instruksi manusia:

| Trigger | Deteksi | Response |
|---------|---------|----------|
| SQL Injection | `UNION SELECT`, tautology, time-based | Block + score +100 |
| XSS | `<script>`, `javascript:`, event handlers | Block + score +60 |
| Path Traversal | `../`, `%2e%2e%2f` | Block + score +100 |
| Command Injection | `; cat`, `| whoami`, `$(` | Block + score +100 |
| Scanner Signature | sqlmap, nikto, nmap, dll | Block + score +100 |
| Honeypot Link | `/trap/xxx` | Instant block |
| Brute Force | 5x login gagal dalam 5 menit | Block |
| Rate Limit | >90 request/min (BIASA), >720 (ADMIN) | Throttle |
| Scanning Velocity | >25 path berbeda dalam 10 detik | Block + score +60 |

### Self-Healing

Setiap intrusion trigger:
1. **Rotate Canaries** — ganti trap links
2. **Tighten WAF** — naikkan sensitivity
3. **Verify Rules** — cek integritas rules
4. **Quarantine** — isolate entity, revoke tokens
5. **Snapshot Evidence** — arsip ke audit chain

### Audit Chain

- Setiap event ditulis ke file hash-chain (`data/audit.jsonl`)
- SHA-256 hash: `hash(n) = SHA256(hash(n-1) + json(event))`
- Kalau chain rusak → quarantine + start new chain
- Single-writer guard: `data/arena.pid`

---

## 3. Trading Agent

### Siklus Kerja

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│ Load Bars   │───►│ Validate     │───►│ Evaluate     │
│ (M15, 3000) │    │ Gate (50 tx) │    │ Signals      │
└─────────────┘    └──────────────┘    └──────┬───────┘
                                              │
                         ┌────────────────────┘
                         ▼
              ┌─────────────────────┐
              │ Risk Check          │
              │ • Daily loss limit  │
              │ • Portfolio heat    │
              │ • Max positions     │
              │ • Class caps        │
              └──────────┬──────────┘
                         │
              ┌──────────▼──────────┐
              │ Execute Order       │
              │ MT5 (live) or       │
              │ Paper (shadow)      │
              └─────────────────────┘
```

### Mode Eksekusi

| Mode | Kondisi | Eksekusi | Sumber Equity |
|------|---------|----------|--------------|
| **PAPER · SIM** | MT5 offline, belum pernah connect | Simulasi | Paper broker ($100k) |
| **MT5 · FEED** | MT5 online, `trade_enabled: false` | Read-only | MT5 live account |
| **MT5 · LIVE** | MT5 online, `trade_enabled: true` | Order nyata ke broker | MT5 live account |
| **PAPER-SHADOW** | MT5 tiba-tiba offline | Shadow book dari balance terakhir | Paper (seeded dari MT5) |

### Auto-Account-Switch

Kalau Anda ganti login di MT5 (akun lain):
1. Agent deteksi `login@server` berubah dalam ~1 detik
2. Reset penuh: equity history, positions, signals
3. Flush shadow book
4. Rebuild universe dari broker catalog baru
5. Re-run gate pada data histori akun baru

---

## 4. Strategi Trading

### 5 Strategi + 1 Ensemble

| Key | Nama | Logika | Edge |
|-----|------|--------|------|
| **sr** | Support/Resistance | Rejection di level swing + breakout-retest | Kaum teknikal |
| **smc** | Smart Money Concepts | Break of structure → order block / FVG retrace | ICT-style |
| **liq** | Liquidity Sweep | Sweep session high/low atau equal highs → reversal | Stop-hunt |
| **macro** | Macro Regime | Tren H4 (EMA20/50) + filter dolar & risk appetite | Fundamental |
| **confluence** | Ensemble Voting | Minimal 2 edge searah pada bar yang sama | Kombinasi |

### Detail Setiap Strategi

#### SR (Support/Resistance)
```python
# Swing level detection: 5-bar fractal
# Buy: price approaches support + bullish rejection candle
# Sell: price approaches resistance + bearish rejection candle
# Filter: ATR > threshold (avoid ranging markets)
```

#### SMC (Smart Money Concepts)
```python
# 1. Identify break of structure (BOS)
# 2. Mark order blocks (OB) / fair value gaps (FVG)
# 3. Wait for price to retrace into OB/FVG
# 4. Entry with stop below structure
```

#### LIQ (Liquidity Sweep)
```python
# 1. Find equal highs / equal lows
# 2. Wait for sweep (wick beyond level)
# 3. Entry on reversal candle close
# 4. Stop beyond sweep wick
```

#### MACRO (Macro Regime)
```python
# 1. Trend: H4 EMA20 vs EMA50
# 2. Dollar filter: DXY direction (inverse EURUSD)
# 3. Risk appetite: Gold returns proxy
# 4. Only trade with macro trend
```

#### CONFLUENCE (Ensemble)
```python
# Voting: count buy/sell signals from other strategies
# Entry only if ≥2 strategies agree
# Weighted by each strategy's historical accuracy
```

---

## 5. Gate / Simulasi

### Proses Gate

```
Strategy + Instrumen ──► Backtest ──► Metrics ──► Verdict
```

### Metrics yang Dihitung

| Metric | Ambang | Penjelasan |
|--------|--------|------------|
| Profit Factor | ≥ 1.25 | Gross profit / gross loss |
| Sharpe Ratio | ≥ 1.0 | Return adjusted for volatility (annualized) |
| Max Drawdown | ≤ 18% | Peak-to-trough decline |
| Win Rate | ≥ 30% | % trades profitable |
| Expectancy | ≥ 0.05R | Expected return per trade in R-multiples |
| Walk-Forward | ≥ 3/4 folds | 4-fold cross-validation, net R > 0 & PF > 1 |
| Monte Carlo | P(profit) ≥ 85% | 2000 random path simulations |
| Trade Count | ≥ 50 | Sample 50 trade terakhir (paling relevan) |

### Status Hasil Gate

```
FAILED ──► PROBATION ──► SIM-VALIDATED ──► LIVE-ELIGIBLE
   │            │              │                │
   │            │              │                └── Data dari broker MT5
   │            │              │
   │            │              └── Data sintetis/simulator
   │            │
   │            └── Score 62-99, 1-3 metrik gagal
   │
   └── Score < 62 atau >3 metrik gagal
```

### Data Sumber

| Sumber | Kapan | Status Tertinggi |
|--------|-------|-----------------|
| Simulator sintetis | MT5 offline | SIM-VALIDATED |
| MT5 M15 history | MT5 online | LIVE-ELIGIBLE |
| CSV file manual | Config override | Tergantung flag |

---

## 6. MetaTrader 5 Integration

### MT5Link Thread

Thread terpisah untuk koneksi MT5 (tidak blok trading thread):

```
[MT5 Thread]           [Trading Thread]
     │                        │
     ├─ initialize() ────────►│
     ├─ account_info() ──────►│
     ├─ positions_get() ─────►│
     ├─ symbol_info() ───────►│
     ├─ bars() ──────────────►│
     │                        │
     │◄─── price() ───────────┤
     │◄─── symbol_map() ──────┤
     │◄─── catalog() ─────────┤
```

### Koneksi States

```
OFF ──► WAITING ──► CONNECTING ──► LIVE ──► ERROR
           │              │            │
           │              │            └── trade_allowed check
           │              │
           └── retry tiap 15 detik   └── auto-reconnect
```

### Data yang Diambil dari MT5

| Data | Sumber MT5 | Frekuensi |
|------|-----------|-----------|
| Balance/Equity | `account_info()` | Tiap 1 detik |
| Positions | `positions_get()` | Tiap 1 detik |
| Closed PnL | `history_deals_get()` | Sekali per hari UTC |
| Price | `symbol_info_tick()` | On-demand |
| Bars | `copy_rates_from_pos(M15)` | Saat gate/load |
| Symbol Map | `symbols_get()` | Tiap 15 detik |

---

## 7. Dashboard Web

### Responsive Breakpoints

| Breakpoint | Layar | Layout |
|------------|-------|--------|
| > 1400px | Desktop | 3 kolom |
| 820-1400px | Laptop | 2 kolom |
| 420-820px | Tablet | 1 kolom |
| < 420px | HP | 1 kolom, compact |

### Panel per Tier

| Panel | DIREKTUR | ADMIN | BIASA |
|-------|----------|-------|-------|
| Equity/PnL KPI | ✅ | ✅ | ❌ |
| Chart equity | ✅ | ✅ | ❌ |
| Posisi terbuka | ✅ | ✅ | ❌ |
| Sinyal trading | ✅ | ✅ | ❌ |
| Security ladder | ✅ | ✅ | ❌ |
| 3-dot menu | ✅ | ✅ | ❌ |
| Kontrol lot/TF | ✅ | ❌ | ❌ |
| Kontrol pause/kill | ✅ | ✅ | ❌ |
| Form banding | ❌ | ❌ | ✅ |
| Appeal review | ✅ | ✅ | ❌ |

### Akses URL

| Dari | URL |
|------|-----|
| PC sendiri | `http://127.0.0.1:8787` |
| HP di WiFi sama | `http://10.45.223.189:8787` |
| VPS cloud | `http://IP_VPS:8787` |

---

## 8. Risk Management

### Per-Trade Limits

| Parameter | Default | DIREKTUR bisa ubah |
|-----------|---------|-------------------|
| Risk per trade | 0.3% equity | ❌ (tetap) |
| Max portfolio heat | 3% | ❌ (tetap) |
| Daily loss limit | 4% | ❌ (tetap) |
| Max open positions | 6 | ❌ (tetap) |
| FOREX cap | 3 positions | ❌ (tetap) |
| COMMODITY cap | 1 position | ❌ (tetap) |

### Kontrol DIREKTUR

| Parameter | Default | Cara Ubah |
|-----------|---------|----------|
| FOREX lot | Auto (risk-based) | Panel direktur / API |
| COMMODITY lot | Auto (risk-based) | Panel direktur / API |
| FOREX timeframe | M15 (default) | Panel direktur / API |
| COMMODITY timeframe | M15 (default) | Panel direktur / API |

### Auto-Halt

| Trigger | Aksi | Auto-release |
|---------|------|-------------|
| Daily loss limit hit | Pause trading | Ya (UTC midnight) |
| Manual pause | Pause trading | Tidak (manual release) |
| Kill switch | Close all + block entry | Tidak (manual release) |
| MT5 AutoTrading OFF | Reject orders | Ya (saat user nyalakan) |

---

## 9. API Endpoints

### Public (No Auth)

| Method | Endpoint | Hasil |
|--------|----------|-------|
| GET | `/` | Gate page (login) |
| GET | `/deck` | Dashboard (if session) |
| POST | `/api/login` | Login dengan key + tier |
| GET | `/api/whoami` | Info entity |
| GET | `/api/health` | Health status |

### Authenticated (Cookie/X-Operator-Key)

| Method | Endpoint | Minimum Tier |
|--------|----------|-------------|
| GET | `/api/state` | BIASA (redacted) |
| GET | `/api/stream` | BIASA (SSE feed) |
| GET | `/api/audit` | ADMIN |
| POST | `/api/security/act` | ADMIN |
| POST | `/api/security/attack` | ADMIN |
| POST | `/api/trading/control` | ADMIN |
| POST | `/api/trading/lot` | DIREKTUR |
| POST | `/api/trading/timeframe` | DIREKTUR |
| POST | `/api/validate` | ADMIN |
| POST | `/api/appeal` | BIASA |
| GET | `/api/appeal/list` | ADMIN |
| POST | `/api/appeal/approve` | ADMIN |
| POST | `/api/recovery` | Recovery token |
| POST | `/api/logout` | Any |

---

## 10. Deployment

### PC Lokal (Windows)

```powershell
# Setup auto-start
.\service\install_autostart.ps1

# Atau dengan akses publik (HP bisa akses)
.\service\install_autostart.ps1 -Public

# Setup 24/7 (no sleep, firewall, dll)
.\tools\setup_24x7.ps1
```

### VPS Cloud

```bash
# 1. Sewa VPS Windows
# 2. Install MT5 + login
# 3. Clone repo
# 4. pip install MetaTrader5
# 5. python run_forever.py --public
```

### GitHub Actions

| Workflow | Trigger | Butuh |
|----------|---------|-------|
| `test.yml` | Push ke main | Tidak ada |
| `pages.yml` | Push web/* | Aktifkan Pages di Settings |
| `deploy.yml` | Manual | VPS_HOST, VPS_USER, VPS_SSH_KEY |
| `health-check.yml` | Manual / Cron 5m | VPS_HOST, DISCORD_WEBHOOK |
| `docker-deploy.yml` | Manual | DOCKER_USERNAME, DOCKER_PASSWORD |

---

## Ringkasan Cepat

| Aspek | Nilai |
|-------|-------|
| Versi | v1.3.0 |
| Strategi | 5 + 1 ensemble |
| Gate sample | 50 trade terakhir |
| Instrumen analisa | Semua dari broker MT5 |
| Max trade simultan | 3 FOREX + 1 COMMODITY |
| Security tier | 4 tier (BLOCKED/BIASA/ADMIN/DIREKTUR) |
| Auto-block | Ya, tanpa instruksi manusia |
| Self-healing | Ya, rotate + tighten + quarantine |
| Audit | Hash-chain SHA-256 |
| Responsive | Ya (420px - 1400px+) |
| MT5 mode | LIVE / FEED / PAPER-SHADOW |
| Auto-switch akun | Ya |

---

*Dokumen ini dibuat untuk SENTINEL ARENA v1.3.0*
