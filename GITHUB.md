# GITHUB + SENTINEL ARENA — Panduan Lengkap

GitHub bukan tempat untuk menjalankan aplikasi 24/7 secara langsung. Tapi GitHub
bisa dipakai untuk **backup kode**, **deploy otomatis ke cloud**, dan **monitoring
gratis**.

---

## Opsi 1: GitHub sebagai Backup & Version Control (GRATIS)

Simpan kode di GitHub supaya aman dan bisa di-restore kalau PC rusak.

### Langkah

```bash
# 1. Buat repository baru di GitHub (misal: sentinel-arena)
# https://github.com/new

# 2. Di folder sentinel-arena Anda:
cd C:\Users\ss22\WorkBuddy AI\2026-10-17-14-10-33\sentinel-arena

git init
git add .
git commit -m "Initial commit - SENTINEL ARENA v1.3.0"

# 3. Push ke GitHub
git remote add origin https://github.com/USERNAME/sentinel-arena.git
git branch -M main
git push -u origin main
```

### Update ke GitHub secara rutin

```bash
git add .
git commit -m "Update: $(date +%Y-%m-%d)"
git push origin main
```

### .gitignore (wajib)

Buat file `.gitignore` supaya file sensitif tidak ikut ke GitHub:

```gitignore
# Data & logs
data/
logs/
*.log
*.pyc
__pycache__/

# State & audit
*.jsonl
data/state.json

# Environment
.env
.venv/
venv/

# IDE
.vscode/
.idea/
*.swp

# OS
Thumbs.db
.DS_Store

# MT5 data
*.csv
```

---

## Opsi 2: GitHub Actions — Deploy Otomatis ke VPS (CI/CD)

Setiap kali Anda push update ke GitHub, server VPS auto-deploy tanpa login manual.

### File `.github/workflows/deploy.yml`

```yaml
name: Deploy to VPS

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Deploy via SSH
        uses: appleboy/ssh-action@v1.0.0
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd /opt/sentinel-arena
            git pull origin main
            systemctl restart sentinel-arena
```

### Setup GitHub Secrets

Di GitHub repository → Settings → Secrets and variables → Actions:

| Secret | Isi |
|---|---|
| `VPS_HOST` | IP VPS Anda (contoh: `203.0.113.100`) |
| `VPS_USER` | Username VPS (contoh: `administrator`) |
| `VPS_SSH_KEY` | Private key SSH (isinya seluruh file `.pem` atau private key) |

### Hasil

Push kode → GitHub Actions deploy otomatis → Agent restart → Update live dalam 1 menit.

---

## Opsi 3: GitHub Actions — Health Check & Alert (GRATIS)

Cek apakah agent masih hidup tiap 5 menit. Kalau mati, kirim notifikasi ke email/Discord.

### File `.github/workflows/health-check.yml`

```yaml
name: Health Check

on:
  schedule:
    - cron: '*/5 * * * *'  # Tiap 5 menit

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Check agent health
        run: |
          STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            http://IP_VPS_ANDA:8787/api/health || echo "000")
          if [ "$STATUS" != "200" ]; then
            echo "AGENT DOWN! Status: $STATUS"
            exit 1
          fi
          echo "Agent OK"

      - name: Notify Discord (if failed)
        if: failure()
        run: |
          curl -X POST ${{ secrets.DISCORD_WEBHOOK }} \
            -H "Content-Type: application/json" \
            -d '{"content": "⚠️ SENTINEL ARENA DOWN! Status check failed."}'
```

### Setup Discord Webhook

1. Buka Discord server Anda
2. Server Settings → Integrations → Webhooks → New Webhook
3. Copy URL webhook
4. Tambah ke GitHub Secrets sebagai `DISCORD_WEBHOOK`

---

## Opsi 4: GitHub + Docker + Cloud (Containerized Deploy)

Package agent sebagai Docker container, deploy ke cloud via GitHub Actions.

### Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && apt-get install -y gcc libpq-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Create data directory
RUN mkdir -p data logs

# Expose port
EXPOSE 8787

# Run agent
CMD ["python", "run_forever.py", "--public"]
```

### File `requirements.txt`

```
MetaTrader5>=5.0.45
```

### File `.github/workflows/docker-deploy.yml`

```yaml
name: Docker Deploy

on:
  push:
    branches: [main]

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Build Docker image
        run: docker build -t sentinel-arena:latest .

      - name: Push to Docker Hub
        run: |
          echo ${{ secrets.DOCKER_PASSWORD }} | docker login -u ${{ secrets.DOCKER_USERNAME }} --password-stdin
          docker tag sentinel-arena:latest ${{ secrets.DOCKER_USERNAME }}/sentinel-arena:latest
          docker push ${{ secrets.DOCKER_USERNAME }}/sentinel-arena:latest

      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.0
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            docker pull ${{ secrets.DOCKER_USERNAME }}/sentinel-arena:latest
            docker stop sentinel-arena || true
            docker rm sentinel-arena || true
            docker run -d --name sentinel-arena \
              -p 8787:8787 \
              -v /opt/sentinel-arena/data:/app/data \
              ${{ secrets.DOCKER_USERNAME }}/sentinel-arena:latest
```

---

## Opsi 5: GitHub Pages — Dashboard Statis (Hanya tampilan, bukan trading)

**PERINGATAN:** GitHub Pages hanya untuk website statis (HTML/CSS/JS). Tidak bisa
menjalankan Python server. Gunakan ini hanya untuk:
- Landing page
- Dokumentasi
- Status page (dengan JavaScript fetch ke VPS)

### File `.github/workflows/pages.yml`

```yaml
name: Deploy to GitHub Pages

on:
  push:
    branches: [main]

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: "pages"
  cancel-in-progress: false

jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Setup Pages
        uses: actions/configure-pages@v4
      - name: Upload artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: './web'
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
```

---

## Opsi 6: GitHub Codespaces (Development, bukan Produksi)

Pakai GitHub Codespaces untuk coding tanpa perlu VS Code lokal. Tapi ini **bukan
untuk produksi 24/7** — Codespaces akan sleep setelah tidak aktif.

### File `.devcontainer/devcontainer.json`

```json
{
  "name": "Sentinel Arena Dev",
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "features": {
    "ghcr.io/devcontainers/features/node:1": {}
  },
  "postCreateCommand": "pip install -r requirements.txt",
  "forwardPorts": [8787],
  "portsAttributes": {
    "8787": {
      "label": "Sentinel Arena",
      "onAutoForward": "notify"
    }
  }
}
```

---

## Ringkasan: GitHub untuk Apa Saja?

| Kebutuhan | Solusi GitHub | Status |
|---|---|---|
| Backup kode | GitHub Repository | ✅ GRATIS |
| Deploy otomatis ke VPS | GitHub Actions + SSH | ✅ GRATIS |
| Health check & alert | GitHub Actions (cron) | ✅ GRATIS |
| Docker deploy | GitHub Actions + Docker Hub | ✅ GRATIS |
| Landing page statis | GitHub Pages | ✅ GRATIS |
| Coding tanpa install | GitHub Codespaces | ✅ GRATIS (60 jam/bulan) |
| **Trading 24/7 langsung** | GitHub (apapun) | ❌ TIDAK BISA |

**Trading 24/7 memang butuh server:** VPS cloud, PC lokal, atau mini PC. GitHub
tidak menyediakan runtime 24/7 untuk aplikasi server.

---

## Setup Cepat: GitHub + VPS

```bash
# 1. Buat repo di GitHub
# 2. Push kode lokal
git init
git remote add origin https://github.com/USERNAME/sentinel-arena.git
git add .
git commit -m "v1.3.0"
git push -u origin main

# 3. Buat folder .github/workflows/
mkdir -p .github/workflows

# 4. Salin file deploy.yml di atas ke .github/workflows/

# 5. Tambah secrets di GitHub Settings

# 6. Push lagi
git add .github
git commit -m "Add auto-deploy"
git push

# 7. Setiap kali push ke main, VPS auto-update!
```

---

*Panduan ini dibuat untuk SENTINEL ARENA v1.3.0*
