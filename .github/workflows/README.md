# GitHub Actions Workflows

Folder ini berisi automation untuk SENTINEL ARENA. Aktifkan di GitHub
Repository → Actions tab.

## Setup Secrets (Wajib)

GitHub Repository → Settings → Secrets and variables → Actions → New repository secret

| Secret | Deskripsi | Contoh |
|---|---|---|
| `VPS_HOST` | IP publik VPS | `203.0.113.100` |
| `VPS_USER` | Username SSH | `administrator` |
| `VPS_SSH_KEY` | Private key SSH (seluruh isi file `.pem`) | `-----BEGIN OPENSSH PRIVATE KEY-----...` |
| `VPS_PORT` | Port SSH (opsional, default 22) | `22` |
| `AGENT_PORT` | Port agent (opsional, default 8787) | `8787` |
| `DISCORD_WEBHOOK` | URL webhook Discord (opsional) | `https://discord.com/api/webhooks/...` |
| `TELEGRAM_BOT_TOKEN` | Token bot Telegram (opsional) | `123456:ABC-DEF...` |
| `TELEGRAM_CHAT_ID` | Chat ID Telegram (opsional) | `-1001234567890` |
| `DOCKER_USERNAME` | Username Docker Hub (opsional) | `username` |
| `DOCKER_PASSWORD` | Password Docker Hub (opsional) | `password` |
| `ARENA_OPERATOR_KEYS` | JSON keys (opsional, untuk Docker) | `{"DIR-xxx":"DIREKTUR"}` |
| `ARENA_PROMOTION_CODE` | Kode promosi (opsional) | `ARENA-DIRECTOR-77` |

## Workflows

### 1. deploy.yml — Auto Deploy ke VPS

**Trigger:** Push ke branch `main` atau `master`

**Yang dilakukan:**
- SSH ke VPS
- Backup `state.json`
- Git pull
- Restart service
- Verifikasi port aktif

**Setup VPS:**
```bash
# Di VPS, clone repo ke /opt/sentinel-arena
cd /opt
sudo git clone https://github.com/USERNAME/sentinel-arena.git
sudo chown -R $USER:$USER sentinel-arena

# Setup systemd service
sudo cp sentinel-arena/service/sentinel-arena.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-arena
```

### 2. health-check.yml — Monitor Agent

**Trigger:** Tiap 5 menit (cron)

**Yang dilakukan:**
- Cek `GET /api/health`
- Kalau status bukan 200 → kirim alert ke Discord/Telegram

**Notifikasi:**
- Discord: butuh `DISCORD_WEBHOOK`
- Telegram: butuh `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`

**Cara dapat Discord webhook:**
1. Buka Discord server → Server Settings
2. Integrations → Webhooks → New Webhook
3. Copy URL

**Cara dapat Telegram bot token:**
1. Chat ke @BotFather di Telegram
2. `/newbot` → ikuti instruksi
3. Copy token

**Cara dapat Telegram chat ID:**
1. Kirim pesan ke bot Anda
2. Buka: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Cari `"chat":{"id":-1001234567890` — angka itu adalah chat ID

### 3. docker-deploy.yml — Build & Deploy Docker

**Trigger:** Push ke `main` atau tag `v*`

**Yang dilakukan:**
- Build Docker image
- Push ke Docker Hub
- Pull dan run di VPS

**Butuh:** `DOCKER_USERNAME`, `DOCKER_PASSWORD`

### 4. pages.yml — Status Page di GitHub Pages

**Trigger:** Push yang mengubah file di `web/`

**Yang dilakukan:**
- Deploy halaman gate statis ke GitHub Pages
- Gunakan sebagai landing page publik

**Aktifkan GitHub Pages:**
1. Repository → Settings → Pages
2. Source: GitHub Actions

## Test Manual

```bash
# Test deploy workflow tanpa push
curl -X POST \
  -H "Authorization: token GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3+json" \
  https://api.github.com/repos/USERNAME/sentinel-arena/actions/workflows/deploy.yml/dispatches \
  -d '{"ref":"main"}'
```

## Troubleshooting

| Masalah | Solusi |
|---|---|
| Deploy gagal SSH | Cek `VPS_SSH_KEY` sudah benar, VPS allow key auth |
| Health check selalu fail | Cek firewall VPS membuka port agent |
| Discord tidak kirim | Cek webhook URL valid, bot punya permission |
| Docker build gagal | Cek `Dockerfile` ada di root, requirements.txt lengkap |
