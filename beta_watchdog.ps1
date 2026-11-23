# SCOPS BETA watchdog: restarts the BETA server if one of its processes dies.
# Started only by "BETA Server.bat" (this file lives only in the beta folder). It never starts or stops the main server:
#  - it only acts while this folder's "SCOPS Server.bat" launcher window is open (the beta is meant to be running);
#  - it ends as soon as that window closes, a normal stop runs (scops_stop.ps1), or BETA Server.bat says stop;
#  - before any restart it checks that no other server (the main one) holds the ports or runs from -MainDir,
#    and if one does it does nothing and ends;
#  - it only kills processes it recorded itself as the beta's, plus the beta's own stack/DNAS windows.
# At most 3 restarts per hour, then it gives up (logged). Log: logs\watchdog.log
param(
  [string]$MainDir = '',
  [int]$CheckSeconds = 15,
  [int]$MaxRestartsPerHour = 3,
  [switch]$EndWatchdog   # used by BETA Server.bat after the beta stops: wait up to 30 s for the watchdog to end, then end it
)
$ErrorActionPreference = 'SilentlyContinue'
if ($EndWatchdog) {
  for ($t = 0; $t -lt 30; $t++) {
    $w = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'beta_watchdog\.ps1' -and $_.CommandLine -notmatch 'EndWatchdog' }
    if (-not $w) { exit 0 }
    Start-Sleep -Seconds 1
  }
  $w | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
  exit 0
}
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log = Join-Path $root 'logs\watchdog.log'
$stopFlag = Join-Path $root 'logs\watchdog.stop'
$stopLog = Join-Path $root 'logs\scops_stop.log'
$pyPattern = 'tools[\\/](nat2[\\/])?(dnas_probe_server|dns_override_forwarder|gs_http_server|tcp_log_server|ubigs_\w+|udp_log_server|udp_reply_server)\.py'
$started = Get-Date
$parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID").ParentProcessId

function Log([string]$msg) {
  try {
    New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
    Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
  } catch {}
}

function Norm([string]$p) { if (-not $p) { return '' }; return ($p.TrimEnd('\') + '\').ToLowerInvariant() }
$rootN = Norm $root
$mainN = Norm $MainDir
if ($mainN -and $mainN -eq $rootN) { Log 'MainDir is this beta folder; watchdog not started.'; exit 0 }

function Launcher {
  # this folder's "SCOPS Server.bat" window (BETA Server.bat runs it as: cmd /c ""<beta folder>\SCOPS Server.bat"")
  Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -match 'SCOPS Server\.bat' -and $_.CommandLine.ToLowerInvariant().Contains($rootN)
  } | Select-Object -First 1
}

function StopRequested {
  if (Test-Path $stopFlag) { return 'BETA Server.bat asked the watchdog to stop' }
  if (-not (Get-Process -Id $parentId)) { return 'BETA Server.bat window closed' }
  if (-not (Launcher)) { return 'the beta launcher window closed' }
  if ((Test-Path $stopLog) -and ((Get-Item $stopLog).LastWriteTime -gt $started)) { return 'the beta was stopped normally' }
  $stopping = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
    $_.CommandLine -match 'scops_stop\.ps1|beta_stop_running\.ps1' -and $_.CommandLine -notmatch 'WatchParent|WaitWatchers' }
  if ($stopping) { return 'a stop is in progress' }
  return $null
}

function MainRunning([hashtable]$ours) {
  # 1. anything started from the main server's folder
  if ($mainN) {
    $m = Get-CimInstance Win32_Process | Where-Object {
      $_.Name -match '^(pythonw?|dnasrep|cmd|powershell)\.exe$' -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($mainN) -and
      ($_.Name -ne 'cmd.exe' -or $_.CommandLine -match 'SCOPS Server\.bat|run_\w+\.bat') -and
      ($_.Name -ne 'powershell.exe' -or $_.CommandLine -match 'start_online_capture|scops_stop|dnasrep') }
    if ($m) { return "a process from the main server folder is running (PID $($m[0].ProcessId))" }
  }
  # 2. the lobby port held by a process the watchdog did not record as the beta's
  $l = Get-NetTCPConnection -State Listen -LocalPort 40005 | Select-Object -First 1
  if ($l -and -not $ours.ContainsKey([int]$l.OwningProcess)) { return "port 40005 is held by PID $($l.OwningProcess), not by the beta" }
  return $null
}

function Snapshot {
  $h = @{}
  Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match $pyPattern } |
    ForEach-Object { $h[[int]$_.ProcessId] = [string]$_.CommandLine }
  Get-CimInstance Win32_Process -Filter "Name='dnasrep.exe'" | ForEach-Object { $h[[int]$_.ProcessId] = 'dnasrep.exe' }
  return $h
}

function ShortName([string]$cmd) {
  if ($cmd -eq 'dnasrep.exe') { return 'DNAS (dnasrep.exe)' }
  $s = $cmd -replace '^.*?(tools[\\/][^ "]+\.py).*$', '$1'
  if ($cmd -match '--ports?\s+"?([0-9,\-]+)') { $s += " port $($Matches[1])" }
  return $s
}

function LogTail([string]$cmd) {
  if ($cmd -match '--log-file\s+"?([^ "]+)') {
    $f = Join-Path $root $Matches[1]
    if (Test-Path $f) {
      Log "  last lines of $($Matches[1]):"
      Get-Content $f -Tail 15 | ForEach-Object { Log ("    " + $_) }
    }
  }
}

function WaitBaseline {
  # wait for the lobby port, then let every service come up and record them
  for ($t = 0; $t -lt 120; $t++) {
    if (StopRequested) { return $null }
    if (Get-NetTCPConnection -State Listen -LocalPort 40005) { break }
    Start-Sleep -Seconds 1
  }
  for ($t = 0; $t -lt 45; $t++) { if (StopRequested) { return $null }; Start-Sleep -Seconds 1 }
  return (Snapshot)
}

function StackName {
  $w = Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match 'run_public_test\.bat|run_nat_even\.bat' } | Select-Object -First 1
  if ($w -and $w.CommandLine -match 'run_nat_even\.bat') { return 'run_nat_even.bat' }
  return 'run_public_test.bat'
}

function KillOurs([hashtable]$ours, [bool]$dnasOnly) {
  foreach ($k in @($ours.Keys)) {
    if ($dnasOnly -and $ours[$k] -ne 'dnasrep.exe') { continue }
    if (Get-Process -Id $k) { taskkill /T /F /PID $k | Out-Null }
  }
  $pat = if ($dnasOnly) { 'run_dnasrep_443_only\.bat' } else { 'run_public_test\.bat|run_nat_even\.bat|run_nat2_test\.bat|run_dnasrep_443_only\.bat' }
  Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" | Where-Object { $_.CommandLine -match $pat } |
    ForEach-Object { taskkill /T /F /PID $_.ProcessId | Out-Null }
  Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'dnasrep\.exe' } |
    ForEach-Object { taskkill /T /F /PID $_.ProcessId | Out-Null }
}

function StartDnas {
  Start-Process -FilePath cmd.exe -WorkingDirectory $root -WindowStyle Hidden -ArgumentList ('/c start "SCOPS DNAS" /d "' + (Join-Path $root 'dnasrep_test') + '" cmd /c run_dnasrep_443_only.bat')
}

function StartStack([string]$stack) {
  Start-Process -FilePath cmd.exe -WorkingDirectory $root -WindowStyle Hidden -ArgumentList ('/c start "SCOPS stack" cmd /c call "' + $stack + '"')
}

Log "---- watchdog started for $root (main server folder: $(if ($MainDir) { $MainDir } else { 'none' })) ----"
# BETA Server.bat starts this just before the beta launcher window; give that window up to 30 s to appear
for ($t = 0; $t -lt 30 -and -not (Launcher); $t++) { Start-Sleep -Seconds 1 }
if (-not (Launcher)) { Log 'the beta launcher window did not appear; watchdog ended.'; exit 0 }
$ours = WaitBaseline
if ($ours -eq $null) { Log "ended before the beta was up: $(StopRequested)"; exit 0 }
$stack = StackName
Log ("watching {0} beta process(es) ({1}): {2}" -f $ours.Count, $stack, (($ours.Values | ForEach-Object { ShortName $_ }) -join ', '))
$restarts = @()
$missCount = 0

while ($true) {
  Start-Sleep -Seconds $CheckSeconds
  $why = StopRequested
  if ($why) { Log "watchdog ended: $why"; exit 0 }
  $gone = @($ours.Keys | Where-Object { -not (Get-Process -Id $_) })
  if ($gone.Count -eq 0) { $missCount = 0; continue }
  $missCount++
  if ($missCount -lt 2) { continue }           # must still be gone on the next check (about 15 s later)

  # it really is gone: check once more that the beta is still meant to run and the main server is not running
  Start-Sleep -Seconds 3
  $why = StopRequested
  if ($why) { Log "watchdog ended: $why"; exit 0 }
  $main = MainRunning $ours
  if ($main) { Log "not restarting: $main. Watchdog ended."; exit 0 }

  $restarts = @($restarts | Where-Object { $_ -gt (Get-Date).AddHours(-1) })
  foreach ($k in $gone) { Log ("STOPPED: " + (ShortName $ours[$k]) + " (PID $k)"); LogTail $ours[$k] }
  if ($restarts.Count -ge $MaxRestartsPerHour) {
    Log "not restarting: already restarted $($restarts.Count) times in the last hour. Watchdog ended; restart the beta by hand."
    exit 0
  }
  $dnasOnly = (@($gone | Where-Object { $ours[$_] -ne 'dnasrep.exe' }).Count -eq 0)
  if ($dnasOnly) {
    Log 'restarting DNAS only'
    KillOurs $ours $true
    Start-Sleep -Seconds 2
    StartDnas
    $d = $null
    for ($t = 0; $t -lt 30; $t++) { Start-Sleep -Seconds 1; $d = Get-CimInstance Win32_Process -Filter "Name='dnasrep.exe'" | Select-Object -First 1; if ($d) { break } }
    foreach ($k in $gone) { $ours.Remove($k) }
    if ($d) { $ours[[int]$d.ProcessId] = 'dnasrep.exe'; Log "DNAS is back (PID $($d.ProcessId))" } else { Log 'DNAS did not come back within 30 s' }
  } else {
    Log "restarting the beta server ($stack + DNAS)"
    KillOurs $ours $false
    for ($t = 0; $t -lt 20; $t++) { if (-not (Get-NetTCPConnection -State Listen -LocalPort 40005)) { break }; Start-Sleep -Seconds 1 }
    StartStack $stack
    for ($t = 0; $t -lt 60; $t++) { Start-Sleep -Seconds 1; if (Get-NetTCPConnection -State Listen -LocalPort 40005) { break } }
    Start-Sleep -Seconds 3
    StartDnas
    $new = WaitBaseline
    if ($new -eq $null) { Log "watchdog ended: $(StopRequested)"; exit 0 }
    $ours = $new
    Log ("beta is back: watching {0} process(es)" -f $ours.Count)
  }
  $restarts += (Get-Date)
  $missCount = 0
}
