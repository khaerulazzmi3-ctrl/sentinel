# SENTINEL ARENA - Windows autostart / 24-7 registration
#
#   powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 -AtStartup -Public
#   powershell -ExecutionPolicy Bypass -File .\service\install_autostart.ps1 -Uninstall
#
# Default: starts at user logon, no admin rights required, restarts on failure.
# -AtStartup needs an elevated shell and starts before anyone logs in.
# -Public binds 0.0.0.0 so other machines on the network can open the dashboard.

param(
    [switch]$AtStartup,
    [switch]$Public,
    [switch]$Uninstall,
    [string]$TaskName = "SentinelArena",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path (Join-Path $root "run_forever.py"))) {
    Write-Host "run_forever.py not found in $root" -ForegroundColor Red
    exit 1
}

# ---- locate a windowless interpreter (pythonw) so no console window is left open
# order: -Python param > $env:ARENA_PYTHON > project venv > PATH
if (-not $Python -and $env:ARENA_PYTHON) { $Python = $env:ARENA_PYTHON }
if (-not $Python) {
    $venv = Join-Path $root ".venv\Scripts\pythonw.exe"
    if (Test-Path $venv) { $Python = $venv }
}
if (-not $Python) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $Python = $cmd.Source
    } else {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $cmd) {
            Write-Host "Python not found in PATH. Pass -Python 'C:\path\to\pythonw.exe'" -ForegroundColor Red
            exit 1
        }
        $candidate = Join-Path (Split-Path -Parent $cmd.Source) "pythonw.exe"
        if (Test-Path $candidate) { $Python = $candidate } else { $Python = $cmd.Source }
    }
}

$arguments = '"' + (Join-Path $root "run_forever.py") + '"'
if ($Public) { $arguments = $arguments + " --public" }

$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $root

if ($AtStartup) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "SENTINEL ARENA - security and trading agents (24/7 web service)" -Force | Out-Null

Write-Host ""
Write-Host "  Registered scheduled task '$TaskName'" -ForegroundColor Green
Write-Host "  python    : $Python"
Write-Host "  arguments : $arguments"
Write-Host "  trigger   : $(if ($AtStartup) { 'at system startup (SYSTEM)' } else { 'at logon (' + $env:USERNAME + ')' })"
Write-Host "  restart   : up to 999 times, 1 minute apart, no time limit"
Write-Host ""
Write-Host "  Start now : Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Status    : Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "  Stop      : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Remove    : .\service\install_autostart.ps1 -Uninstall"
Write-Host ""
