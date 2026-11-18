# Fully stops a running SCOPS server (main or beta) so the other one can start on the same ports.
# Unlike a plain scops_stop.ps1 run, it also closes the old "SCOPS Server.bat" launcher window and its
# -WatchParent cleanup watcher. Otherwise that watcher would fire later (when someone closes the old
# window) and kill whichever server is running at that moment.
#   -WaitWatchers : only wait until no scops_stop.ps1 is running any more (used after the beta stops).
param([switch]$WaitWatchers)
$ErrorActionPreference = 'SilentlyContinue'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Wait-NoStopScripts([int]$seconds) {
  for ($t = 0; $t -lt $seconds; $t++) {
    $busy = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
      Where-Object { $_.CommandLine -match 'scops_stop\.ps1' -and $_.ProcessId -ne $PID }
    if (-not $busy) { return $true }
    Start-Sleep -Seconds 1
  }
  return $false
}

if ($WaitWatchers) {
  if (Wait-NoStopScripts 30) { exit 0 } else { exit 1 }
}

# 1. the cleanup watchers first, so they cannot fire later
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -match 'scops_stop\.ps1' -and $_.CommandLine -match 'WatchParent' } |
  ForEach-Object { taskkill /F /PID $_.ProcessId | Out-Null; Write-Host "  closed cleanup watcher $($_.ProcessId)" }

# 2. the old launcher window(s) running "SCOPS Server.bat" (this script runs before the beta's own one starts)
Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" |
  Where-Object { $_.CommandLine -match 'SCOPS Server\.bat' } |
  ForEach-Object { taskkill /T /F /PID $_.ProcessId | Out-Null; Write-Host "  closed launcher window $($_.ProcessId)" }

# 3. the server itself (stack, Python processes, DNAS)
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here 'scops_stop.ps1')

# 4. wait for the lobby port to be free
for ($t = 0; $t -lt 20; $t++) {
  if (-not (Get-NetTCPConnection -State Listen -LocalPort 40005)) { exit 0 }
  Start-Sleep -Seconds 1
}
exit 1
