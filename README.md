# SENTINEL ARENA

Dua AI agent yang berjalan di satu command deck:

1. **SENTINEL** — agent keamanan dengan 4 tingkatan akses, auto-block, dan self-healing.
2. **ATLAS** — agent trading untuk forex & komoditas (S/R, SMC, liquidity, macro, confluence)
   yang **hanya boleh trading setelah strateginya lolos uji simulasi**.

Dashboard berbasis game menampilkan PnL live, posisi, feed ancaman, ladder akses, dan
matriks gate strategi secara real-time (SSE).

```
python run.py                 →  http://127.0.0.1:8787     (manual)
python run_forever.py         →  auto-restart 24/7
python run_forever.py --public →  bisa diakses dari mesin lain di jaringan
```

Tanpa dependency eksternal. Python 3.10+. Opsional: `pip install MetaTrader5` untuk PnL live dari akun MT5.

**Dirancang untuk hidup terus.** Proses dijaga supervisor, state disimpan ke disk, audit
chain tidak putus saat restart, dan `/api/health` bisa dipantau uptime-monitor.
Lihat [bagian 8](#8-menjalankan-247).

**Agent mengeksekusi sendiri.** Strategi yang lulus gate langsung dipakai untuk
membuka order di akun MT5 Anda (`mt5.trade_enabled: true`), satu entry per
instrumen, maksimal 3 forex + 1 komoditas sekaligus, dan hanya pada instrumen
yang benar-benar ditawarkan broker Anda. Syarat di sisi terminal: tombol
**Algo Trading** harus menyala — kalau belum, semua order ditolak broker dengan
`10027` dan dashboard menampilkan banner kuning sebagai peringatan
(lihat [4.2](#42-trade_enabled--feed-read-only-atau-eksekusi-sendiri)).

**Kalau Anda sendiri diblokir** (termasuk saat login dengan key DIREKTUR), itu perilaku
yang benar — gate berjalan sebelum login. Jalur keluarnya ada di
[bagian 9](#9-cara-mendapat-hak-direktur-dan-apa-yang-terjadi-kalau-anda-diblokir).

---

## 1. Arsitektur

```
                    ┌──────────────────────────────┐
   browser  ──────► │  HTTP + SSE  (server/app.py)  │
                    └──────────┬───────────────────┘
                               │ setiap request diperiksa dulu
                    ┌──────────▼───────────────────┐
                    │  SENTINEL  (security agent)  │
                    │  deteksi → skor → blokir     │
                    │  canary → self-heal          │
                    │  audit hash-chain            │
                    └──────────┬───────────────────┘
                               │ tier check (ADMIN+ untuk aksi)
                    ┌──────────▼───────────────────┐
                    │  ATLAS  (trading agent)      │
                    │  gate → risk → eksekusi      │
                    └──────┬──────────────┬────────┘
                           │              │
                  ┌────────▼──────┐  ┌────▼─────────────┐
                  │ market data   │  │ broker            │
                  │ synthetic /   │  │ paper / MT5       │
                  │ CSV / MT5     │  │                   │
                  └───────────────┘  └───────────────────┘
```

Modul:

| Path | Isi |
|---|---|
| `security/agent.py` | detektor intrusi, tier ladder, auto-block, self-heal, audit chain |
| `core/store.py` | snapshot atomik, penjaga penulis tunggal (`data/arena.pid`), karantina audit |
| `trading/strategies.py` | S/R, SMC, liquidity sweep, macro regime, confluence ensemble |
| `trading/backtest.py` | engine backtest, walk-forward 4 fold, Monte Carlo 2000 path, gate + sampel 50 trade |
| `trading/broker.py` | paper broker, `MT5Broker` (order nyata), `RouterBroker` (pilih venue per order) |
| `trading/mt5link.py` | thread koneksi MT5: akun, posisi, harga, bar M15, pemetaan simbol broker |
| `trading/agent.py` | orkestrasi validasi → universe broker → eksekusi → risk control |
| `trading/datafeed.py` | simulator pasar regime-switching, loader CSV, adopsi bar MT5 |
| `server/app.py` | HTTP/SSE, semua request lewat security agent, redaksi BIASA |
| `web/` | dashboard responsive (vanilla JS, canvas, tanpa framework) + halaman gate |
| `tools/setup_24x7.ps1` | setup otomatis: no-sleep, firewall, Cloudflare Tunnel |
| `DEPLOY.md` | panduan deploy ke VPS cloud / PC 24/7 / tunnel |
| `GITHUB.md` | panduan GitHub: backup, CI/CD deploy, health check |

---

## 2. Model keamanan — 4 tingkatan

```
BLOKIR (0)  <  BIASA (1)  <  ADMIN (2)  <  DIREKTUR (3)
```

Aturan yang **ditegakkan di kode**, bukan di UI:

| Aksi | BIASA | ADMIN | DIREKTUR |
|---|:--:|:--:|:--:|
| Melihat dashboard | ya | ya | ya |
| Pause / kill switch trading | — | ya | ya |
| Simulasi serangan (red team) | — | ya | ya |
| **BLOKIR → BIASA** (cabut blokir) | — | ya | ya |
| **BLOKIR → ADMIN** | — | ya | ya |
| **BIASA → ADMIN** | — | ya | ya |
| ADMIN → BIASA / BLOKIR | — | ya | ya |
| **Menjadikan DIREKTUR** | — | — | ya + kode konfirmasi |
| Mengubah entitas yang sudah DIREKTUR | — | — | ya |

Ringkasnya: **ADMIN bisa menaikkan sampai ADMIN** (dari BLOKIR, BIASA, atau
menurunkan kembali), dan **hanya DIREKTUR yang bisa mengangkat DIREKTUR** —
termasuk dirinya sendiri butuh kode konfirmasi. ADMIN tidak bisa menyentuh
entitas yang sudah DIREKTUR sama sekali.

### 2.1 Kunci operator

Kunci ada di `config.json` → `security.operator_keys`, atau ditimpa lewat
environment `ARENA_OPERATOR_KEYS`. Kunci inilah yang menentukan tier Anda.

| Tier | API key |
|---|---|
| DIREKTUR | `DIR-R69E-T72D-8GL3` |
| ADMIN | `ADM-HGZF-RGT6-X4H9` |
| BIASA | `USR-7EZF-FR5L-ZQHV` |

Untuk instalasi nyata, jangan simpan kunci di file — pakai environment:

```bash
export ARENA_OPERATOR_KEYS='{"DIR-XXXX-XXXX-XXXX":"DIREKTUR","ADM-XXXX-XXXX-XXXX":"ADMIN","USR-XXXX-XXXX-XXXX":"BIASA"}'
export ARENA_PROMOTION_CODE='kode-rahasia-anda'
```

Kunci **tidak pernah** dikembalikan oleh API (`/api/state` dan `/api/whoami`
hanya mengembalikan tier + level), tidak masuk URL, dan tidak disimpan di
`localStorage`. Setelah login, otoritas dibawa oleh cookie `HttpOnly;
SameSite=Strict` selama 12 jam.

### 2.2 Halaman masuk (gate)

Membuka `http://127.0.0.1:8787/` menampilkan halaman gate, bukan dashboard:

```
API KEY   [ DIR-XXXX-XXXX-XXXX ]

MASUK SEBAGAI
  01 BIASA     lihat dashboard, tanpa aksi
  02 ADMIN     cabut blokir, naikkan ke BIASA/ADMIN, pause & kill
  03 DIREKTUR  semua aksi, termasuk mengangkat DIREKTUR

[ MASUK ]
```

Kunci dan tier **harus cocok**: kunci BIASA yang mengklik DIREKTUR ditolak 403
(`Kunci ini terdaftar sebagai BIASA, bukan DIREKTUR`) dan percobaan itu masuk
audit sebagai `LOGIN_TIER_MISMATCH`. Tombol tier hanya memilih cara Anda masuk,
bukan menaikkan hak — hak selalu dibaca dari kuncinya. Gate ini juga yang
membuat Anda tidak "terblokir tanpa alasan": halaman menampilkan entitas, tier,
dan status blokir IP Anda sebelum Anda mengetik apa pun.

**Blokir otomatis tanpa instruksi admin.** Setiap request diperiksa sebelum routing:
SQLi/XSS/traversal/RCE, signature tool scanner, probe path sensitif, honeypot,
rate limit, dan velocity scanning. Skor ≥ 60 atau satu deteksi critical → identitas
langsung `BLOKIR`, sesi dikarantina, token dicabut, dan audit dicatat.

**Canary link.** `/trap/<token>` adalah tautan jebakan. Siapa pun yang membukanya
diblokir seketika — tidak ada tombol "approve", tidak ada masa tenggang. Token
di-rotasi otomatis tiap 120 detik dan setiap kali ada intrusi.

**Self-healing.** Saat intrusi terdeteksi agent menjalankan remediasi dan mencatat
setiap langkah: rotasi canary, naikkan sensitivitas WAF, verifikasi hash ruleset
terhadap baseline (mendeteksi tampering), karantina sesi, snapshot bukti. Bila
ruleset berubah tanpa otorisasi, baseline dipulihkan dan dilaporkan.

**Audit hash-chain.** Setiap keputusan masuk ke log berantai SHA-256. Mengubah satu
baris membuat `verify_chain()` gagal dan dashboard menampilkan `CHAIN ALERT`.

**Satu penulis saja.** Chain hanya bermakna kalau hanya satu proses yang menulis.
`data/arena.pid` menandai pemiliknya; proses lain yang mengimpor agent (skrip
diagnostik, instance kedua) otomatis jadi read-only dan tidak menyentuh file.
Kalau saat boot rantai tidak lolos verifikasi, sistem **tidak** menimpanya:
file dipindahkan ke `data/audit.quarantine-<ts>.jsonl` sebagai bukti, lalu chain
baru dimulai dengan entri `CHAIN_QUARANTINE` yang menyebut file buktinya.

**Tidak ada auto-unblock.** Identitas yang diblokir tetap diblokir sampai ada
perintah eksplisit ADMIN/DIREKTUR. Tidak ada timer, tidak ada probation.

**Unblock benar-benar membebaskan.** Saat tier dinaikkan dari BLOKIR, agent juga
membersihkan penyebabnya: hitungan gagal login, jendela rate limit, skor risiko,
daftar deteksi, dan token sesi lama. Tanpa ini, operator yang baru dipulihkan
akan langsung kena throttle/block lagi dan mengira pemulihannya gagal.

---

## 3. Gate strategi — tidak ada jalan pintas ke live

Setiap pasangan **(strategi × instrumen)** diuji terpisah. Sebuah strategi hanya
boleh dipakai jika **semua** syarat lulus:

| Pemeriksaan | Ambang |
|---|---|
| Sampel trade | **50 trade terakhir** (paling relevan dengan kondisi pasar sekarang) |
| Profit factor | ≥ 1.25 |
| Sharpe (annualised) | ≥ 1.0 |
| Max drawdown | ≤ 18% |
| Win rate | ≥ 30% |
| Expectancy | ≥ 0.05 R |
| Walk-forward 4 fold | ≥ 3 fold profitabel (net R > 0, PF > 1) |
| Monte Carlo 2000 path | P(profit) ≥ 85% |

**Kenapa 50 trade terakhir, bukan seluruh histori?** Backtest bisa menghasilkan
ratusan trade dari data lama yang karakternya sudah tidak ada lagi (volatilitas,
spread, sesi). Setelah backtest selesai, simulator mengambil **50 trade terakhir**
— yaitu yang paling dekat dengan kondisi pasar saat ini — lalu **semua metrik di
atas dihitung ulang dari sampel itu** (equity curve, drawdown, PF, Sharpe,
expectancy, walk-forward, Monte Carlo). Kalau trade yang tersedia kurang dari 50,
pair tersebut **tidak** bisa lulus: `min_trades` juga 50. Angka ini diatur di
`config.json`:

```json
"gate": { "sample_trades": 50, "min_trades": 50, "min_mc_probability": 0.85 }
```

Di dashboard, sel yang lulus menampilkan `sample_of` — berapa trade total yang
ditemukan sebelum dipotong ke 50 — supaya Anda bisa menilai apakah sampelnya
cukup mewakili.

Status: `FAILED` → `PROBATION` (skor 62–99) → `SIM-VALIDATED` → `LIVE-ELIGIBLE`
(hanya jika data uji berasal dari feed broker, bukan sintetis).

Strategi yang belum lulus **tidak pernah** membuka posisi. Yang lulus otomatis
diizinkan trading (bisa dijeda / kill switch oleh ADMIN).

Strategi:

| Key | Logika |
|---|---|
| `sr` | rejection di level swing + breakout-retest (kausal, tanpa look-ahead) |
| `smc` | break of structure → order block / fair value gap retrace |
| `liq` | sweep session high/low atau equal highs → reversal stop-hunt |
| `macro` | tren H4 (EMA20/50) + filter dolar & risk appetite lintas aset |
| `confluence` | voting ensemble, minimal 2 edge searah pada bar yang sama |

### 3.1 Instrumen yang boleh dipakai

Universe **bukan** daftar keinginan — ia diambil dari apa yang benar-benar
ditawarkan broker Anda ke terminal MT5. Saat tersambung, agent memetakan setiap
simbol di `config.json` ke nama broker (mis. `XAUUSD` → `XAUUSDc`), lalu:

- simbol yang **tidak ada** di broker dibuang dari universe, dari matriks
  strategi, dan tidak pernah dikirim sebagai order — kalau broker Anda tidak
  punya WTIUSD, WTIUSD tidak muncul di dashboard dan tidak pernah ditradingkan;
- bar historis dan harga live diambil dari simbol broker yang benar (bukan dari
  data sintetis), sehingga gate dan sinyal memakai harga yang sama dengan yang
  Anda lihat di terminal;
- strip **INSTRUMEN BROKER** di atas matriks menampilkan pemetaan itu
  (`EURUSD → EURUSDc`) beserta batas per kelas dan alasan sebuah instrumen
  sedang dilewati.

### 3.2 Batas posisi & eksekusi otomatis

| Aturan | Nilai |
|---|---|
| Maksimum posisi terbuka | 6 |
| Maksimum forex sekaligus | **3** (`trading.max_per_class.FOREX`) |
| Maksimum commodity sekaligus | **1** (`trading.max_per_class.COMMODITY`) |
| Entry per instrumen | **1** — tidak ada order menumpuk |
| Entry per pair per bar | 1 (tidak ada re-entry storm setelah kena SL) |
| Re-entry setelah TP/SL | **boleh**, karena posisi sudah tertutup |

Agent **mengeksekusi sendiri** order untuk strategi yang lulus gate
(`mt5.trade_enabled: true`). Risiko per trade 0.3% equity
(`trading.risk_per_trade_pct`), portfolio heat ≤ 3%
(`trading.max_portfolio_heat_pct`), daily loss limit 4% (`trading.daily_loss_limit_pct`,
auto-halt), kill switch menutup semua posisi.

Posisi yang dibuka manual di terminal ikut dihitung: kalau Anda sudah punya
posisi `XAUUSDc`, agent **tidak** akan menambah posisi XAUUSD (batas commodity 1
sudah terpakai) dan alasannya tampil di strip instrumen. Begitu Anda menutupnya
(TP/SL/manual), instrumen itu terbuka lagi untuk agent.

Ukuran lot dihitung dari equity akun live, bukan dari saldo kertas. Kalau
`allow_min_lot: true` dan hasil hitung di bawah lot minimum broker, order tetap
dikirim dengan lot minimum **hanya jika** risikonya masih di bawah
`mt5.max_risk_pct_hard` (default 2%); kalau tidak, order ditolak dan alasannya
muncul di feed sinyal.

---

## 4. PnL live & MetaTrader 5

Sistem punya dua sumber angka, dan dashboard selalu menyebut yang mana:

| Mode | Sumber equity/PnL | Eksekusi |
|---|---|---|
| `PAPER · SIM` | paper broker $100.000 | simulasi |
| `MT5 · FEED` | **akun MT5 Anda, live** | read-only — order tidak dikirim |
| `MT5 · LIVE` | akun MT5 Anda, live | order benar-benar dikirim ke broker |
| `PAPER-SHADOW` | buku bayangan, di-seed dari balance live terakhir | agent tetap trading walau terminal mati |

Angka equity, unrealized, margin, dan mata uang diambil apa adanya dari akun
MT5 (`USC`, `USD`, `EUR`, …) — dashboard menampilkan `USC 370.49`, bukan
mengasumsikan dolar.

### 4.1 Menyambung ke akun MT5

```bash
python -m venv .venv
.venv/Scripts/pip install MetaTrader5      # Windows saja
```

Lalu di `config.json`:

```json
"mt5": {
  "enabled": true,
  "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
  "login": 0,                // 0 = pakai akun yang sudah login di terminal
  "password": "",
  "server": "",
  "symbol_suffix": "",       // opsional: "c", ".m", dst
  "magic": 771001,
  "trade_enabled": true,     // false = feed read-only, true = agent eksekusi sendiri
  "allow_min_lot": true,     // boleh turun ke lot minimum kalau modal kecil
  "max_risk_pct_hard": 2.0,  // batas risiko keras untuk lot minimum
  "poll_seconds": 1.0,
  "retry_seconds": 15.0
}
```

**Tidak perlu restart.** Koneksi berjalan di thread sendiri: kalau terminal belum
terbuka, state-nya `WAITING` dan sistem mencoba lagi tiap 15 detik. Begitu MT5
dibuka dan login, dashboard berpindah sendiri ke `MT5 · FEED` dan kurva equity
di-reset ke akun nyata (peristiwa ini juga tercatat di feed log).

Yang dibaca dari terminal: `balance`, `equity`, `margin`, `currency`, `login`,
`server`, seluruh posisi terbuka (termasuk posisi manual yang Anda buka sendiri
di terminal), dan PnL tertutup hari ini dari `history_deals_get`.

### 4.2 `trade_enabled` — feed read-only atau eksekusi sendiri

Dengan `trade_enabled: false`, ATLAS **tidak mengirim order**. Ia tetap
mengevaluasi strategi dan menampilkan sinyal (`status: SIGNAL`, lengkap dengan
SL/TP/RR) supaya Anda bisa eksekusi manual, sementara PnL dashboard 100% dari
akun MT5 Anda. Ini yang aman untuk akun sungguhan.

Set `trade_enabled: true` kalau Anda ingin agent mengirim order nyata — ini yang
dipakai sekarang (`config.json` sudah `true`, dan agent benar-benar mengeksekusi
strategi yang lulus gate tanpa Anda klik apa pun). Saat itu `magic` 771001
dipakai supaya posisi agent tidak tercampur dengan posisi manual Anda, dan
dashboard menandai `MT5 · LIVE`.

#### AutoTrading di terminal — satu tombol yang menentukan

MT5 punya saklar keselamatan sendiri. Selama tombol **Algo Trading** di toolbar
terminal masih OFF, broker akan menolak setiap order robot dengan
`10027 AutoTrading disabled by client`, tidak peduli apa pun isi `config.json`.
Agent tidak bisa menyalakannya dari luar (memang dirancang begitu), jadi:

1. buka terminal MT5 → klik **Algo Trading** di toolbar sampai ikonnya menyala;
2. pastikan juga *Tools → Options → Expert Advisors → Allow Algo Trading* aktif.

Setelah itu tidak perlu restart apa pun. Dashboard akan menghapus banner kuning
**AUTOTRADING MT5 MASIH OFF** dalam ~1 detik, dan order berikutnya terkirim
sendiri. Selama tombol itu OFF, agent tetap memindai pasar dan mencatat setiap
sinyal sebagai `REJECTED` dengan alasan yang sama — jadi Anda bisa melihat ia
memang mau masuk, hanya pintunya yang belum dibuka.

Status saklar itu bisa dicek kapan saja:

```bash
curl -s http://127.0.0.1:8787/api/health | grep -o '"trade_allowed": [a-z]*'
```

Catatan akun cent: mata uang `USC` ditampilkan apa adanya (`USC 370.49`), tidak
dipaksa jadi `$` — simbol mata uang diambil dari `account_info().currency`.

### 4.3 Kalau sambungan gagal

Sistem **tidak** diam-diam pindah mode. `/api/health` dan panel dashboard
menampilkan state (`WAITING` / `ERROR` / `LIVE`) beserta alasannya, misalnya
`terminal tidak menjawab (-10003)`. Penyebab paling umum: terminal belum dibuka,
belum login akun, atau paket `MetaTrader5` belum terpasang di interpreter yang
menjalankan service (lihat 8.2 — interpreter-nya harus venv yang sama).

Untuk data historis nyata pada gate strategi: begitu MT5 tersambung, agent
mengadopsi **3000 bar M15 langsung dari broker** (`mt5.history_bars`) untuk setiap
instrumen di universe (bukan lagi sintetis), dan gate memakai bar yang sama dengan
yang Anda lihat di terminal. Itulah sebabnya status tertinggi `LIVE-ELIGIBLE` bisa
dicapai hanya saat `data_source` = `mt5-live`.

Dua hal yang dijaga di sini, karena keduanya pernah bikin gate "buta":

- **Restart tidak boleh mematikan data nyata.** Pemetaan simbol broker disimpan di
  state, jadi setelah restart agent **tetap** memuat ulang bar broker (dulu
  pemetaan yang sama membuat proses ini dilewati, gate diam-diam kembali ke data
  sintetis, dan tidak ada pair yang bisa `LIVE-ELIGIBLE`).
- **Histori yang masih dangkal diperdalam.** MT5 mengunduh histori secara
  bertahap: sambungan pertama kadang hanya memberi sebagian bar, sehingga sebuah
  pair cuma menghasilkan 33 trade dan mustahil memenuhi sampel 50 trade. Agent
  memeriksa kedalaman tiap 15 detik dan, kalau masih kurang dari 80% target,
  mengadopsi ulang histori lalu menjalankan gate sekali lagi. Nilainya terlihat di
  `/api/state` sebagai `history_depth` / `history_bars`.

Cadangan `data/<SYMBOL>.csv` (kolom `time,open,high,low,close,volume`) tetap
didukung untuk menguji di luar jam pasar atau tanpa terminal; selama masih
sintetis, status tertinggi hanya `SIM-VALIDATED`.

---

## 5. API

| Method | Path | Fungsi |
|---|---|---|
| GET | `/api/state` | snapshot penuh (security + trading) |
| GET | `/api/stream` | SSE, 1 update/detik (pakai cookie sesi) |
| GET | `/api/health` | heartbeat + status ringkas + state MT5; **503** bila engine macet |
| GET | `/api/audit` | audit chain + status verifikasi + jumlah baris tersimpan |
| GET | `/api/whoami` | sesi aktif + tier entitas IP ini (dipakai halaman gate) |
| POST | `/api/login` | `{key,tier}` → cookie `HttpOnly` sesi 12 jam; tier harus cocok |
| POST | `/api/logout` | hapus sesi |
| POST | `/api/recovery` | break-glass `{token,target,tier,reason}` — tetap jalan walau IP diblokir |
| POST | `/api/security/act` | ubah tier `{target,tier,reason,code}` |
| POST | `/api/security/attack` | red-team `{kind}` → canary/sqli/scanner/traversal/bruteforce |
| POST | `/api/trading/control` | `{action}` → pause/resume/kill/revalidate |
| POST | `/api/validate` | uji ulang satu pasangan `{strategy,symbol}` |

Halaman: `/` → gate (atau dashboard bila sesi sudah ada), `/deck` → dashboard
(wajib sesi; tanpa sesi dialihkan ke gate).

Tidak ada operator key di URL: key ditukar sekali lewat `/api/login`, lalu
otorisasi memakai cookie `HttpOnly; SameSite=Strict` — jadi tidak ada rahasia
yang bocor ke history browser atau access log. Klien API tetap bisa memakai
header `X-Operator-Key`.

---

## 6. Batasan yang perlu Anda tahu

- **Data sintetis hanya dipakai bila MT5 tidak tersambung.** Dengan terminal
  hidup, bar M15 diambil dari broker Anda; tanpa terminal, simulator meniru
  regime switching, volatility clustering, sesi likuiditas, dan stop-hunt, dan
  status `SIM-VALIDATED` sengaja dibedakan dari `LIVE-ELIGIBLE`.
- **Gate tidak menolong strategi yang jelek.** Pada data M15 nyata, `sr` lulus
  (PF 1.43–1.48) sementara `confluence`, `liq`, `macro`, dan `smc` gagal di
  sebagian besar pair. Itu hasil yang jujur — ambangnya tidak diturunkan supaya
  semuanya "hijau".
- **Backtest bukan jaminan.** Gate menolak strategi yang tidak stabil, tapi
  masa lalu tidak menjamin masa depan. Risiko tetap ada.
- **Paper broker memakai model risk-normalized PnL**, bukan tick-by-tick
  order book. Untuk uji produksi, jalankan dengan akun demo MT5.
- **Bukan nasihat keuangan.** Gunakan akun demo sebelum dana nyata.
- Dashboard ini alat operasional, bukan produk berizin. Kepatuhan regulator
  (Bappebti/OJK untuk Indonesia) adalah tanggung jawab operator.

---

## 7. Uji cepat

```bash
python tests_security.py       # butuh server berjalan
node tests_ui.js               # render dashboard di jsdom (snapshot diambil live)
node tests_gate.js             # halaman masuk: key + pilihan tier
```

`tests_ui.js` mengambil snapshot sendiri dari `/api/state` (override dengan
`ARENA_KEY=...`), jadi tidak ada fixture yang perlu di-commit.

Menguji: auto-block SQLi & brute force, penolakan akses untuk identitas diblokir,
matriks otoritas tier (ADMIN bisa naik ke ADMIN tapi tidak bisa mengangkat
DIREKTUR), kecocokan kunci↔tier saat login, kontrol trading, validitas audit
chain, dan break-glass recovery.

Total saat ini: **29 cek keamanan/API**, **23 cek dashboard**, **11 cek gate**.
Dua cek dashboard khusus menjaga permintaan Anda: jumlah sel matriks harus sama
dengan ukuran universe, dan tidak boleh ada `WTIUSD`/`XAGUSD`/`COPPER` di
dashboard kalau broker tidak menawarkannya.

---

## 8. Menjalankan 24/7

Sistem ini memang dirancang untuk menyala terus: satu proses supervisor, satu proses
server, state di disk, log berotasi. Tidak ada database yang perlu dirawat.

### 8.1 Tiga lapis ketahanan

| Lapis | Apa yang dijaga |
|---|---|
| `run_forever.py` | anak proses mati → dijalankan ulang, backoff eksponensial (maks 60 s) supaya crash-loop tidak membakar CPU |
| Windows Task Scheduler / systemd | supervisor mati (reboot, logoff, OOM) → dijalankan ulang otomatis |
| `/api/health` | mengembalikan **503** kalau heartbeat engine lebih tua dari 30 detik, jadi uptime-monitor bisa membedakan "sepi" dan "mati" |

### 8.2 Windows — daftarkan autostart

```powershell
# jalan saat user login (tanpa hak admin)
powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1

# jalan saat boot, sebelum login (butuh shell elevated)
powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 -AtStartup

# sekalian bisa diakses dari LAN
powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 -Public

# cek / hentikan / hapus
Get-ScheduledTaskInfo -TaskName SentinelArena
Stop-ScheduledTask   -TaskName SentinelArena
powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 -Uninstall
```

Task didaftarkan dengan `RestartCount 999`, interval 1 menit, dan tanpa batas waktu
eksekusi. `pythonw.exe` dipakai supaya tidak ada jendela konsol menggantung.

**Interpreter harus yang punya paket MetaTrader5.** Kalau Anda membuat venv,
arahkan task ke venv itu — kalau tidak, service akan jalan tapi MT5 berstatus
`ERROR` (`paket MetaTrader5 belum terpasang`) walaupun `python` di shell Anda
sudah bisa mengimpornya:

```powershell
powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 `
  -Python "C:\path\to\sentinel-arena\.venv\Scripts\pythonw.exe"
```

Skrip memilih interpreter dengan urutan: `-Python` → `%ARENA_PYTHON%` →
`.venv\Scripts\pythonw.exe` di folder proyek → `pythonw` di PATH.

### 8.3 Linux — systemd

```bash
sudo cp -r sentinel-arena /opt/sentinel-arena
sudo cp /opt/sentinel-arena/service/sentinel-arena.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-arena
journalctl -u sentinel-arena -f
```

### 8.4 Akses dari mesin lain

```bash
python run_forever.py --public        # bind 0.0.0.0:8787
```

Default tetap `127.0.0.1`. Kalau dibuka ke jaringan:

1. Setiap request tetap melewati security agent, dan **semua orang harus login**
   dengan operator key — tidak ada halaman yang terbuka tanpa sesi.
2. Ganti key demo. Jangan simpan di `config.json`; pakai environment variable:

```powershell
$env:ARENA_OPERATOR_KEYS = '{"dir-XXXX":"DIREKTUR","adm-XXXX":"ADMIN"}'
$env:ARENA_PROMOTION_CODE = "kode-rahasia-anda"
python run_forever.py --public
```

3. Untuk akses dari internet, taruh di belakang reverse proxy dengan TLS
   (Caddy/Nginx) atau tunnel (Cloudflare Tunnel). Jangan expose port 8787 polos —
   cookie sesi memakai `HttpOnly; SameSite=Strict`, tapi tanpa TLS tetap bisa disadap.
4. Batasi firewall ke subnet tepercaya.

### 8.5 Apa yang bertahan saat restart

| Bertahan | Hilang |
|---|---|
| Entitas, tier, status blokir, riwayat | Sesi login (harus login ulang) |
| Audit hash-chain (append-only, lanjut nomor urut) | Sinyal & log in-memory |
| Saldo paper, posisi terbuka, riwayat equity | Cache hasil validasi (dihitung ulang saat boot) |
| Status pause / kill switch / halt harian | Kanari lama (di-rotasi saat start) |

Snapshot ditulis tiap 15 detik dan saat shutdown ke `data/state.json` (atomik:
tulis `.tmp` lalu `os.replace`). Audit ditulis baris-per-baris ke
`data/audit.jsonl` dan dirotasi pada 8 MB. Log server di `logs/arena.log`
(rotasi 5 MB × 5), supervisor di `logs/supervisor.log`.

### 8.6 Perilaku halt yang benar untuk jangka panjang

- **Daily loss limit** berhenti otomatis saat sesi UTC berganti — batas harian
  memang kedaluwarsa harian.
- **Pause manual** dan **kill switch** **tidak** berhenti sendiri, termasuk setelah
  restart. Hanya operator ADMIN/DIREKTUR yang bisa melepas.
- Posisi yang masih terbuka saat restart diadopsi kembali oleh paper broker,
  jadi tidak ada risiko yang "hilang" diam-diam.

### 8.7 Tiga hal yang sering ditanyakan soal "tetap hidup"

| Situasi | Yang terjadi |
|---|---|
| **Tidak ada sinyal** | Loop tetap berjalan tiap bar; tidak ada yang perlu dilakukan. `heartbeat` tetap segar, `/api/health` tetap `200`, dashboard tetap `TRADING`. "Tidak ada trade" bukan "mati". |
| **Terminal MT5 ditutup** | Agent pindah ke `PAPER-SHADOW`: buku bayangan di-seed dari balance live terakhir, strategi yang lulus tetap dieksekusi di sana, dan begitu terminal kembali semua posisi bayangan diratakan (`SWITCH_TO_MT5`) lalu eksekusi kembali ke akun nyata. Banner kuning menandai mode ini. |
| **PC mati / reboot** | Task Scheduler (atau systemd) menyalakan lagi supervisor → server → agent, state dibaca dari `data/state.json`, audit chain diverifikasi dulu (kalau bercabang, file dikarantina dan chain baru dimulai), lalu trading lanjut. Yang tidak otomatis: MT5 sendiri harus ikut autostart dan login, kalau tidak agent tetap hidup di `PAPER-SHADOW`. |

Satu penjaga tambahan: hanya **satu** proses yang boleh menulis `data/audit.jsonl`
(`data/arena.pid`). Kalau Anda menjalankan perintah Python lain yang mengimpor
modul server saat service hidup, proses itu jadi read-only dan tidak bisa
merusak hash-chain — dulu ini pernah memecah rantai audit, sekarang tidak bisa lagi.

### 8.8 Benar-benar 24/7 — PC mati pun tetap jalan?

**Realitas teknis:** kalau PC benar-benar **power off**, tidak ada software lokal
yang bisa berjalan. Tapi ada 3 solusi:

| Solusi | Cara | Biaya |
|---|---|---|
| **A. PC tetap nyala + optimasi** | Matikan sleep/hibernate, pakai UPS, auto-start setelah listrik padam | Rp 800rb-2jt (UPS) |
| **B. VPS Cloud Windows** | Sewa VPS di cloud, install MT5, jalankan agent di sana | $20-40/bulan |
| **C. Cloudflare Tunnel** | PC lokal tetap nyala, tapi akses via URL HTTPS publik | Gratis |

Untuk panduan lengkap semua opsi, baca **[DEPLOY.md](DEPLOY.md)**.

Setup cepat PC lokal (PowerShell admin):

```powershell
.\tools\setup_24x7.ps1
```

Setup + Cloudflare Tunnel (dapat URL publik gratis):

```powershell
.\tools\setup_24x7.ps1 -CloudflareTunnel
```

---

## 9. Cara mendapat hak DIREKTUR (dan apa yang terjadi kalau Anda diblokir)

Ini pertanyaan paling penting, karena memang ada dua hal berbeda yang sering
tertukar: **operator key** (siapa Anda) dan **tier entitas** (apakah IP/perangkat
Anda boleh lewat).

### 9.1 Dua hal yang berbeda

| | Operator key | Tier entitas |
|---|---|---|
| Apa itu | String rahasia yang Anda ketik di halaman login | Status IP/perangkat di mata security agent |
| Disimpan di | `config.json` → `security.operator_keys`, atau env `ARENA_OPERATOR_KEYS` | `data/state.json` (persisten) |
| Menentukan | Tier Anda **setelah** lolos gate | Apakah request Anda **boleh masuk** gate |
| Bisa dicabut oleh | Anda sendiri (ganti key) | Security agent (otomatis) atau ADMIN/DIREKTUR |

Kuncinya: **gate berjalan sebelum login**. Jadi kalau IP Anda berstatus
`BLOCKED`, mengetik key DIREKTUR yang benar tetap ditolak — bukan karena key-nya
salah, tapi karena permintaannya tidak pernah sampai ke tahap pemeriksaan key.
Ini memang disengaja: kalau key yang benar bisa menembus blokir, maka blokir
tidak ada artinya (penyerang tinggal menebak atau mencuri key).

Karena itu ada **jalur pemulihan** di luar aplikasi.

### 9.2 Tiga jalur pemulihan

**Jalur A — break-glass token (utama, tanpa mematikan server)**

Saat pertama kali boot, sistem membuat token acak 32 byte di
`data/recovery.key` (permission 0600; di Windows ACL-nya dikunci ke user Anda).
Token ini **tidak pernah** dikirim ke browser dan tidak ada di repo. Gunakan CLI:

```bash
python tools/arena_admin.py status
python tools/arena_admin.py entities
python tools/arena_admin.py unblock 203.0.113.9
python tools/arena_admin.py promote 203.0.113.9 DIREKTUR
```

Setiap perubahan lewat jalur ini tercatat di audit hash-chain dengan
`actor: "RECOVERY"` dan `action: "BREAK_GLASS"`, jadi tidak bisa dipakai
diam-diam. Endpoint `/api/recovery` adalah satu-satunya POST yang tetap
diizinkan walaupun pemanggilnya sedang `BLOCKED` — dan hanya bisa dipakai kalau
Anda memegang token di disk.

**Jalur B — environment variable (menetapkan DIREKTUR pertama)**

Kalau belum punya key DIREKTUR sama sekali, set sebelum start:

```bash
export ARENA_OPERATOR_KEYS='{"dir-XXXX":"DIREKTUR","adm-XXXX":"ADMIN"}'
export ARENA_PROMOTION_CODE='kode-rahasia-anda'
```

Nilai di environment **menimpa** isi `config.json`, jadi key demo
(`dir-9F4A-SENTINEL`) otomatis mati. Ini cara yang benar untuk instalasi nyata.

**Jalur C — edit state offline (hanya kalau server mati)**

Hentikan service, lalu:

```bash
python tools/arena_admin.py unblock 203.0.113.9
```

CLI akan menolak menulis langsung ke `data/state.json` kalau file itu baru
diperbarui kurang dari 20 detik lalu (tanda server masih hidup dan akan menimpa
perubahan Anda). Matikan dulu, baru jalankan.

### 9.3 Urutan yang disarankan untuk instalasi baru

1. Start sekali, lihat log: key demo dan lokasi `data/recovery.key` dicetak.
2. **Salin isi `data/recovery.key`** ke tempat aman (password manager / offline).
3. Set `ARENA_OPERATOR_KEYS` dengan key DIREKTUR milik Anda sendiri, restart.
4. Login, lalu naikkan tier perangkat Anda: `promote <ip-anda> DIREKTUR`.
5. Baru setelah itu lakukan uji serangan (`/api/security/attack`) — kalau Anda
   memblokir diri sendiri, Jalur A selalu tersedia.

### 9.4 Kalau Anda membuka halaman dan melihat ACCESS DENIED

Halaman 403 itu bukan error server. Artinya security agent sudah memutuskan
sebelum ada instruksi admin — sesuai desain. Yang perlu dilakukan:

1. Cek alasan di halaman tersebut (ada `reason`, `score`, dan daftar deteksi).
2. Dari mesin lain (atau shell lokal), jalankan `arena_admin.py entities` untuk
   melihat status, lalu `unblock` bila memang Anda.
3. Kalau ini serangan sungguhan: **jangan** unblock. Biarkan `BLOCKED`,
   lihat `/api/audit` untuk jejaknya, dan rotasi operator key.

### 9.5 Melindungi `data/recovery.key`

- Jangan commit (sudah masuk `.gitignore` lewat `data/`).
- Jangan taruh di folder yang dibagikan/sinkron ke cloud publik.
- Kalau token bocor: hapus filenya, restart — token baru dibuat otomatis.
  Token lama langsung tidak berlaku karena dibaca ulang dari disk setiap request.
- Token ini **hanya** untuk pemulihan tier. Ia tidak memberi akses dashboard,
  tidak bisa dipakai membaca posisi/PnL, dan tidak menggantikan operator key.

