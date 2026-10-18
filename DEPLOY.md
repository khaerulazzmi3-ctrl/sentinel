# DEPLOYMENT 24/7 — SENTINEL ARENA

> **Realitas teknis:** Kalau PC Anda benar-benar **mati total (power off)**, tidak ada software lokal yang bisa berjalan. Tapi ada 3 cara supaya agent tetap hidup dan dashboard tetap bisa diakses.

---

## Opsi 1: VPS Cloud Windows (REKOMENDASI)

**Cara kerja:** Sewa VPS Windows di cloud → install MT5 → jalankan agent di sana → dashboard akses dari mana saja via browser.

### Provider yang cocok

| Provider | VPS Windows | Harga/bulan | Link |
|---|---|---|---|
| **Vultr** | Windows Server 2022 | ~$20-40 | vultr.com |
| **DigitalOcean** | Droplet Windows | ~$24-48 | digitalocean.com |
| **AWS Lightsail** | Windows Server | ~$20-40 | aws.amazon.com/lightsail |
| **Azure** | B2s Windows | ~$30-60 | azure.microsoft.com |
| **Contabo** | VPS Windows | ~$8-15 | contabo.com |

### Langkah Setup

```powershell
# 1. Connect ke VPS Windows via RDP
# 2. Download dan install MetaTrader 5
# 3. Login ke akun trading di MT5
# 4. Download Python 3.12 dari python.org
# 5. Clone/copy folder sentinel-arena ke VPS

# 6. Install dependency
pip install MetaTrader5

# 7. Jalankan service (powershell as admin)
cd C:\sentinel-arena
.\service\install_autostart.ps1

# 8. Buka firewall port 8787
New-NetFirewallRule -DisplayName "SentinelArena" -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow

# 9. Akses dashboard dari HP/laptop Anda:
# http://IP_VPS_ANDA:8787
```

### Keamanan VPS

```powershell
# Ganti ke port non-standar (opsional tapi direkomendasikan)
# Edit config.json: "port": 18787

# Setup reverse proxy dengan TLS (Cloudflare Tunnel)
# Download cloudflared.exe dan run:
cloudflared tunnel --url http://localhost:8787
# Akan dapat URL https://xxxx.trycloudflare.com yang bisa dibuka dari HP
```

---

## Opsi 2: PC Lokal + Anti-Mati

Kalau Anda ingin tetap pakai PC yang ada tapi jangan sampai mati:

### A. Matikan Sleep/Hibernate

```powershell
# PowerShell as Admin
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /h off
powercfg /change monitor-timeout-ac 30
```

### B. Auto-Start Setelah Listrik Padam

```powershell
# BIOS: aktifkan "Restore on AC Power Loss" -> Power On
# Windows: Task Scheduler -> SentinelArena sudah auto-start
```

### C. Gunakan UPS (Uninterruptible Power Supply)

Rekomendasi untuk PC trading:
- **APC Back-UPS 650VA** (~Rp 800rb) - tahan ~15 menit
- **APC Back-UPS Pro 1500VA** (~Rp 2jt) - tahan ~30-45 menit
- **CyberPower CP1500** (~Rp 2.5jt) - dengan LCD monitoring

UPS memberi waktu:
1. Simpan posisi
2. Close MT5 dengan aman
3. Shutdown PC bersih
4. Saat listrik kembali → PC auto-on → agent auto-start

### D. Akses dari Luar Jaringan (Port Forwarding)

**Router Anda harus support port forwarding:**

1. Buka router admin (biasa 192.168.1.1 atau 192.168.0.1)
2. Cari menu "Port Forwarding" / "Virtual Server"
3. Tambah rule:
   - External Port: 8787 (atau port lain)
   - Internal IP: IP PC Anda (cek dengan `ipconfig`)
   - Internal Port: 8787
   - Protocol: TCP
4. Catat IP publik Anda: `curl ifconfig.me`
5. Akses dari HP: `http://IP_PUBLIK:8787`

**CATATAN:** Gunakan ini hanya di jaringan yang aman. Tanpa TLS, data bisa disadap.

---

## Opsi 3: Cloudflare Tunnel (AMAN + MUDAH)

**Cara kerja:** PC lokal tetap nyala, tapi akses dashboard lewat URL HTTPS publik — tanpa port forwarding.

### Setup

```powershell
# 1. Download cloudflared
# https://github.com/cloudflare/cloudflared/releases

# 2. Extract ke C:\cloudflared\

# 3. Buka CMD/PowerShell
cd C:\cloudflared
cloudflared.exe tunnel --url http://localhost:8787

# 4. Akan muncul URL seperti:
# https://sentinel-arena-xyz.trycloudflare.com
# URL ini bisa dibuka dari HP, laptop, mana saja!
```

### Persistent Tunnel (auto-start)

```powershell
# Login ke Cloudflare
C:\cloudflared\cloudflared.exe tunnel login

# Buat tunnel
C:\cloudflared\cloudflared.exe tunnel create sentinel-arena

# Config
$conf = @"
tunnel: <TUNNEL_ID>
credentials-file: C:\cloudflared\.cloudflared\<TUNNEL_ID>.json

ingress:
  - hostname: arena-namaanda.yourdomain.com
    service: http://localhost:8787
  - service: http_status:404
"@
$conf | Out-File -Encoding utf8 C:\cloudflared\config.yml

# Install as service
C:\cloudflared\cloudflared.exe service install

# Start service
Start-Service cloudflared
```

---

## Opsi 4: Mini PC / Raspberry Pi (Low Power 24/7)

Kalau Anda punya:
- **Intel NUC** (~Rp 3-5jt)
- **Beelink Mini S12** (~Rp 2-3jt)
- **Raspberry Pi 4/5** (~Rp 1-2jt) - tapi butuh Linux + Wine untuk MT5

Keunggulan: daya listrik kecil (~10-15W), bisa nyala 24/7 tanpa khawatir biaya listrik besar.

### Setup Mini PC Windows

Sama dengan Opsi 2 (PC Lokal), tapi device khusus yang tidak pernah dimatikan.

---

## Ringkasan: Pilih Sesuai Kebutuhan

| Kebutuhan | Solusi | Biaya |
|---|---|---|
| Budget minim, PC sudah ada | PC Lokal + No Sleep + Port Forward | Gratis |
| Budget minim, akses aman dari HP | PC Lokal + Cloudflare Tunnel | Gratis |
| Mau benar-benar 24/7 tanpa khawatir | VPS Cloud Windows | $20-40/bulan |
| Daya listrik kecil, hardware sendiri | Mini PC / NUC | Rp 2-5jt sekali |
| Ada domain sendiri + TLS | VPS + Domain + Nginx/Caddy | $25-50/bulan |

---

## Checklist Sebelum Deploy

- [ ] MT5 sudah terinstall dan login di target device
- [ ] AutoTrading di MT5 sudah ON (Algo Trading button)
- [ ] Firewall membuka port 8787 (atau port yang dipilih)
- [ ] Task Scheduler / service auto-start sudah aktif
- [ ] API keys sudah diatur di config.json
- [ ] Backup config.json dan data/ secara berkala
- [ ] Test akses dashboard dari HP sebelum dianggap "siap"

---

## Monitoring dari Jarak Jauh

Setelah deploy, pantau agent dari HP:

```
# Health check
curl http://IP_ANDA:8787/api/health

# State
curl http://IP_ANDA:8787/api/state
```

Atau pasang uptime monitor gratis:
- **UptimeRobot** (uptimerobot.com) - check tiap 5 menit gratis
- **Pingdom** - check HTTP 200 ke /api/health
- **Better Uptime** - notifikasi Telegram/Discord kalau agent mati

---

*Versi dokumen: v1.0 — dibuat untuk SENTINEL ARENA v1.3.0*
