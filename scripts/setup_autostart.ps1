# Install the Windows-side bridge as a Scheduled Task that starts at logon and
# pushes usage to the watch over BLE. Re-runnable (Force overwrites). This is
# the Windows counterpart of setup_autostart.sh (macOS LaunchAgent).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_autostart.ps1 [-IntervalMinutes 5]
# Uninstall:
#   Unregister-ScheduledTask -TaskName "CCIslandBridge"
#
# No admin rights needed: the task is registered for the current user only.
param([int]$IntervalMinutes = 5)

$ErrorActionPreference = "Stop"

$repo   = Split-Path -Parent $PSScriptRoot
$bridge = Join-Path $repo "bridge\codexisland_bridge.py"
$log    = Join-Path $repo "bridge.log"

# --- 1) Locate Python (python.org install on PATH; Store stub won't work) ----
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3.exe -ErrorAction SilentlyContinue).Source }
if (-not $py -or $py -like "*WindowsApps*") {
    throw "Python not found. Install 64-bit Python 3.11+ from python.org and re-run."
}

# --- 2) Dependencies (user level, same as the macOS script) -------------------
# bleak: BLE on Win11 ships as 1.x (needs build 22000+); Win10 must stay on 0.22.x.
& $py -m pip install --user --quiet --disable-pip-version-check certifi
$build = [Environment]::OSVersion.Version.Build
if ($build -ge 22000) {
    & $py -m pip install --user --quiet --disable-pip-version-check --upgrade bleak
} else {
    Write-Host "Windows 10 (build $build) detected: pinning bleak 0.22.x (bleak-winrt backend)."
    & $py -m pip install --user --quiet --disable-pip-version-check "bleak==0.22.*"
}

# --- 3) pythonw.exe = no console window; logs go to --log-file ---------------
$pyw = Join-Path (Split-Path $py) "pythonw.exe"
if (-not (Test-Path $pyw)) { $pyw = $py }

# --- 4) Register the task: logon trigger, run forever, restart on failure ----
# (Equivalent of the LaunchAgent's RunAtLoad + KeepAlive + ThrottleInterval.)
$action   = New-ScheduledTaskAction -Execute $pyw `
              -Argument "-u `"$bridge`" --ble $IntervalMinutes --log-file `"$log`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
              -RestartInterval (New-TimeSpan -Minutes 1) `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "CCIslandBridge" -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "CC Island BLE bridge (Claude/Codex/GLM/DeepSeek usage -> M5 StopWatch)" | Out-Null

Write-Host ""
Write-Host "==> Done. The bridge now runs at logon (task 'CCIslandBridge')."
Write-Host "    Open the CC Island app on the watch so it starts advertising."
Write-Host "    First data check:  python `"$bridge`" --json"
Write-Host "    Run now:           Start-ScheduledTask -TaskName CCIslandBridge"
Write-Host "    Logs:              $log"
Write-Host "    Uninstall:         Unregister-ScheduledTask -TaskName CCIslandBridge"
