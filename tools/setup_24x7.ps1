#requires -RunAsAdministrator
<#
.SYNOPSIS
    Setup otomatis agar SENTINEL ARENA berjalan 24/7 di PC lokal.
.DESCRIPTION
    - Matikan sleep/hibernate
    - Setup auto-start setelah power loss (jika BIOS support)
    - Install Cloudflare Tunnel (opsional)
    - Setup firewall rule
    - Verify service status
#>
param(
    [switch]$CloudflareTunnel,
    [string]$CloudflaredPath = "C:\cloudflared",
    [int]$Port = 8787
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  SENTINEL ARENA — 24/7 Setup Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 1. Matikan Sleep/Hibernate
Write-Host "[1/5] Mematikan sleep dan hibernate..." -ForegroundColor Yellow
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /h off
powercfg /change monitor-timeout-ac 30
powercfg /change monitor-timeout-dc 10
Write-Host "      Sleep dan hibernate DIMATIKAN." -ForegroundColor Green
Write-Host "      Monitor akan mati setelah 30 menit (AC) / 10 menit (baterai)." -ForegroundColor Gray
Write-Host ""

# 2. Firewall rule
Write-Host "[2/5] Setup firewall..." -ForegroundColor Yellow
$rule = Get-NetFirewallRule -DisplayName "SentinelArena" -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName "SentinelArena" -Direction Inbound `
        -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Write-Host "      Firewall rule 'SentinelArena' port $Port DITAMBAHKAN." -ForegroundColor Green
} else {
    Write-Host "      Firewall rule sudah ada." -ForegroundColor Gray
}
Write-Host ""

# 3. Cek Scheduled Task
Write-Host "[3/5] Cek scheduled task SentinelArena..." -ForegroundColor Yellow
$task = Get-ScheduledTask -TaskName "SentinelArena" -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "      Task 'SentinelArena' ditemukan — state: $($task.State)" -ForegroundColor Green
    if ($task.State -eq "Ready") {
        Start-ScheduledTask -TaskName "SentinelArena"
        Write-Host "      Task DIJALANKAN." -ForegroundColor Green
    }
} else {
    Write-Host "      Task BELUM ada. Jalankan .\service\install_autostart.ps1 dulu." -ForegroundColor Red
}
Write-Host ""

# 4. Cek service listening
Write-Host "[4/5] Cek apakah port $Port sudah listen..." -ForegroundColor Yellow
Start-Sleep -Seconds 3
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conn) {
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    Write-Host "      Port $Port aktif — PID $($conn.OwningProcess) ($($proc.ProcessName))" -ForegroundColor Green
} else {
    Write-Host "      Port $Port BELUM aktif. Service mungkin masih startup." -ForegroundColor Red
}
Write-Host ""

# 5. Info jaringan
Write-Host "[5/5] Info jaringan..." -ForegroundColor Yellow
$localIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*"
} | Select-Object -First 1).IPAddress
Write-Host "      IP Lokal   : $localIP" -ForegroundColor Cyan
Write-Host "      Port       : $Port" -ForegroundColor Cyan
Write-Host "      URL Lokal  : http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "      URL LAN    : http://${localIP}:$Port" -ForegroundColor Cyan
Write-Host ""

# 6. Cloudflare Tunnel (opsional)
if ($CloudflareTunnel) {
    Write-Host "[BONUS] Setup Cloudflare Tunnel..." -ForegroundColor Yellow
    if (-not (Test-Path $CloudflaredPath)) {
        New-Item -ItemType Directory -Path $CloudflaredPath | Out-Null
    }
    $cfExe = Join-Path $CloudflaredPath "cloudflared.exe"
    if (-not (Test-Path $cfExe)) {
        Write-Host "      Downloading cloudflared.exe..." -ForegroundColor Gray
        $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
        try {
            Invoke-WebRequest -Uri $url -OutFile $cfExe -UseBasicParsing
            Write-Host "      cloudflared.exe BERHASIL didownload." -ForegroundColor Green
        } catch {
            Write-Host "      GAGAL download. Silakan download manual dari:" -ForegroundColor Red
            Write-Host "      https://github.com/cloudflare/cloudflared/releases" -ForegroundColor Gray
        }
    }
    if (Test-Path $cfExe) {
        Write-Host "      Menjalankan Cloudflare Tunnel..." -ForegroundColor Gray
        Write-Host "      URL publik akan muncul di bawah (tunggu 5-10 detik):" -ForegroundColor Cyan
        Start-Process -FilePath $cfExe -ArgumentList "tunnel","--url","http://localhost:$Port" -NoNewWindow
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  SETUP SELESAI!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Akses dashboard:" -ForegroundColor White
Write-Host "  - Dari PC ini : http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  - Dari LAN    : http://${localIP}:$Port" -ForegroundColor Cyan
if ($CloudflareTunnel) {
    Write-Host "  - Dari Internet: (lihat URL dari cloudflared di atas)" -ForegroundColor Cyan
}
Write-Host ""
Write-Host "Tips agar PC tidak mati:" -ForegroundColor White
Write-Host "  1. Sambungkan ke UPS (Uninterruptible Power Supply)" -ForegroundColor Gray
Write-Host "  2. Atur BIOS: Power On After Power Loss = ON" -ForegroundColor Gray
Write-Host "  3. Jangan matikan monitor dengan tombol power — biarkan timeout" -ForegroundColor Gray
Write-Host ""
Write-Host "Untuk deploy ke VPS Cloud, baca DEPLOY.md" -ForegroundColor Gray
Write-Host ""
