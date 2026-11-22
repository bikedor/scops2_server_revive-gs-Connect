# SCOPS_UPnP_Check.ps1 - Splinter Cell Chaos Theory (PS2 / PCSX2) host helper with a window. V8.18
# Asks for the PS2 address, checks whether the router on this network supports UPnP, and if it does,
# opens the host ports (UDP 3658 and 10070-10080) to that address while the window stays open.
# After each add it reads the mapping back. Green (and the open hint to the SCOPS server) only when
# the router confirms every port points at the typed PS2 address, not the PC that asked.
# Talks to the router directly (SSDP + SOAP), not through the Windows HNetCfg COM object, which often
# reports "no router" even when UPnP is on.
# Forwards are made with a 1-hour lease and renewed every 20 minutes, so they expire on their own
# if this window is killed. Closing the window normally removes them at once.
# Launch with SCOPS_UPnP_Check.bat.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# One SCOPS window at a time (V8.13). A second window used to close the first window's DNS check as a "leftover"
# process, so the first one sat on "Checking DNS..." with no answer until it was reopened.
$script:OneWindow = New-Object System.Threading.Mutex($false, "Local\SCOPS_Window_SCCT")
$script:OwnWindow = $false
try { $script:OwnWindow = $script:OneWindow.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $script:OwnWindow = $true }
if (-not $script:OwnWindow) {
    [void][System.Windows.Forms.MessageBox]::Show("The SCOPS window is already open. Use that one (look on the taskbar).",
        "SCOPS", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
    exit
}

$script:UpnpLib = @'
$GamePorts = @(3658) + (10070..10080)
$MapDesc = "SCCT-PS2"
$LeaseS = 3600

function Test-PrivateIp([string]$ip) {
    if (-not $ip -or $ip -eq "0.0.0.0") { return $true }
    $o = $ip.Split(".") | ForEach-Object { [int]$_ }
    if ($o.Count -ne 4) { return $true }
    if ($o[0] -eq 10) { return $true }
    if ($o[0] -eq 172 -and $o[1] -ge 16 -and $o[1] -le 31) { return $true }
    if ($o[0] -eq 192 -and $o[1] -eq 168) { return $true }
    if ($o[0] -eq 100 -and $o[1] -ge 64 -and $o[1] -le 127) { return $true }
    if ($o[0] -eq 169 -and $o[1] -eq 254) { return $true }
    return $false
}

function Get-PcAdapters {
    # Every network card that is up with an IPv4 address, for the "PC network card" list (idea from PSRewired's
    # RetroDNS adapter picker). Real Wi-Fi/Ethernet cards with a gateway come first, the one carrying internet traffic
    # at the top; VPN and virtual adapters (ZeroTier, Tailscale, Hamachi, TAP/TUN, Hyper-V, VirtualBox, VMware,
    # Bluetooth, ...) are listed last and marked, because a PS2 or PCSX2 on the home network cannot reach them.
    $route = $null
    try {
        $u = New-Object System.Net.Sockets.UdpClient
        $u.Connect("8.8.8.8", 53)          # UDP connect sends nothing; it only picks the route
        $route = $u.Client.LocalEndPoint.Address.ToString()
        $u.Close()
    } catch {}
    $virt = "ZeroTier|Tailscale|Hamachi|Radmin|WireGuard|Wintun|OpenVPN|\bTAP\b|\bTUN\b|\bVPN\b|NordLynx|Proton|Mullvad|" +
            "Hyper-V|vEthernet|VirtualBox|VMware|Virtual|Bluetooth|Loopback|Docker|\bWSL\b|Teredo|6to4|isatap"
    $list = @()
    foreach ($n in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($n.OperationalStatus -ne "Up") { continue }
        if ($n.NetworkInterfaceType -eq "Loopback" -or $n.NetworkInterfaceType -eq "Tunnel") { continue }
        $p = $n.GetIPProperties()
        $gw = $p.GatewayAddresses | Where-Object { $_.Address.AddressFamily -eq "InterNetwork" -and
                                                   $_.Address.ToString() -ne "0.0.0.0" } | Select-Object -First 1
        foreach ($ua in $p.UnicastAddresses) {
            if ($ua.Address.AddressFamily -ne "InterNetwork") { continue }
            $ip = $ua.Address.ToString()
            if ($ip.StartsWith("169.254.") -or $ip.StartsWith("127.")) { continue }
            $isVirt = ("$($n.Name) $($n.Description)" -match $virt)
            $kind = if ($n.NetworkInterfaceType -eq "Wireless80211") { "Wi-Fi" } elseif ($isVirt) { "VPN/virtual" } else { "Ethernet" }
            $rank = if ($isVirt) { 3 } elseif (-not $gw) { 2 } elseif ($ip -eq $route) { 0 } else { 1 }
            $list += [pscustomobject]@{ Ip = $ip; Name = $n.Description; Kind = $kind; Virtual = $isVirt;
                                        HasGw = [bool]$gw; Rank = $rank }
        }
    }
    return @($list | Sort-Object Rank, Ip)
}

function Get-LanCandidates {
    # Every IPv4 address on an adapter that is up and has a gateway. The adapter that carries the
    # internet route comes first; a VPN adapter usually cannot see the home router, so all are tried.
    $first = $null
    try {
        $u = New-Object System.Net.Sockets.UdpClient
        $u.Connect("8.8.8.8", 53)          # UDP connect sends nothing; it only picks the route
        $first = $u.Client.LocalEndPoint.Address.ToString()
        $u.Close()
    } catch {}
    $list = @()
    foreach ($n in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        if ($n.OperationalStatus -ne "Up") { continue }
        if ($n.NetworkInterfaceType -eq "Loopback") { continue }
        $p = $n.GetIPProperties()
        $gw = $p.GatewayAddresses | Where-Object { $_.Address.AddressFamily -eq "InterNetwork" -and
                                                   $_.Address.ToString() -ne "0.0.0.0" } | Select-Object -First 1
        if (-not $gw) { continue }
        foreach ($ua in $p.UnicastAddresses) {
            if ($ua.Address.AddressFamily -ne "InterNetwork") { continue }
            $list += [pscustomobject]@{ Ip = $ua.Address.ToString(); Gw = $gw.Address.ToString();
                                        Mask = "$($ua.IPv4Mask)"; Name = $n.Description }
        }
    }
    $sorted = @($list | Where-Object { $_.Ip -eq $first }) + @($list | Where-Object { $_.Ip -ne $first })
    return $sorted
}

function Find-Igd($cands, [int]$waitMs = 2500) {
    $sts = @("urn:schemas-upnp-org:device:InternetGatewayDevice:1",
             "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
             "urn:schemas-upnp-org:service:WANIPConnection:1",
             "urn:schemas-upnp-org:service:WANPPPConnection:1")
    $found = [ordered]@{}
    $group = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Parse("239.255.255.250"), 1900)
    foreach ($c in $cands) {
        $sock = $null
        try {
            $sock = [System.Net.Sockets.UdpClient]::new([System.Net.IPEndPoint]::new([System.Net.IPAddress]::Parse($c.Ip), 0))
            $sock.Client.ReceiveTimeout = 300
            foreach ($st in $sts) {
                $q = "M-SEARCH * HTTP/1.1`r`nHOST: 239.255.255.250:1900`r`nMAN: `"ssdp:discover`"`r`nMX: 2`r`nST: $st`r`n`r`n"
                $b = [System.Text.Encoding]::ASCII.GetBytes($q)
                [void]$sock.Send($b, $b.Length, $group)
            }
            $end = (Get-Date).AddMilliseconds($waitMs)
            while ((Get-Date) -lt $end) {
                $from = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)
                try { $data = $sock.Receive([ref]$from) } catch { continue }
                $txt = [System.Text.Encoding]::ASCII.GetString($data)
                if ($txt -match "(?im)^location:\s*(\S+)") {
                    $loc = $Matches[1]
                    if (-not $found.Contains($loc)) { $found[$loc] = $c }
                }
            }
        } catch {
        } finally {
            if ($sock) { $sock.Close() }
        }
        if ($found.Count -gt 0) { break }
    }
    return $found
}

function Invoke-Http([string]$url, [string]$body = $null, [string]$soapAction = $null, [int]$timeoutMs = 5000) {
    $req = [System.Net.HttpWebRequest]::Create($url)
    $req.Timeout = $timeoutMs
    $req.ReadWriteTimeout = $timeoutMs
    $req.Proxy = $null
    $req.KeepAlive = $false
    if ($body) {
        $req.Method = "POST"
        $req.ContentType = 'text/xml; charset="utf-8"'
        $req.Headers.Add("SOAPAction", "`"$soapAction`"")
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
        $req.ContentLength = $bytes.Length
    }
    $resp = $null
    try {
        if ($body) {
            $s = $req.GetRequestStream()
            $s.Write($bytes, 0, $bytes.Length)
            $s.Close()
        }
        $resp = $req.GetResponse()
    } catch {
        $ex = $_.Exception
        while ($ex -and -not ($ex -is [System.Net.WebException])) { $ex = $ex.InnerException }
        if ($ex -and $ex.Response) { $resp = $ex.Response }
        else { return @{ Code = -1; Text = "$($_.Exception.Message)" } }
    }
    $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
    $txt = $sr.ReadToEnd()
    $code = [int]$resp.StatusCode
    $resp.Close()
    return @{ Code = $code; Text = $txt }
}

function Get-WanService([string]$location) {
    $r = Invoke-Http $location
    if ($r.Code -ne 200) { return $null }
    try { $x = [xml]$r.Text } catch { return $null }
    $base = $x.SelectSingleNode("//*[local-name()='URLBase']")
    $baseUri = if ($base -and $base.InnerText.Trim()) { [Uri]$base.InnerText.Trim() } else { [Uri]$location }
    $best = $null
    foreach ($s in $x.SelectNodes("//*[local-name()='service']")) {
        $t = $s.SelectSingleNode("*[local-name()='serviceType']")
        $c = $s.SelectSingleNode("*[local-name()='controlURL']")
        if (-not $t -or -not $c) { continue }
        $type = $t.InnerText.Trim()
        if ($type -match "WANIPConnection|WANPPPConnection") {
            $svc = [pscustomobject]@{ Type = $type; Ctl = ([Uri]::new($baseUri, $c.InnerText.Trim())).AbsoluteUri;
                                      Name = ""; Make = ""; Model = "" }
            $fn = $x.SelectSingleNode("//*[local-name()='friendlyName']")
            if ($fn) { $svc.Name = $fn.InnerText.Trim() }
            $mk = $x.SelectSingleNode("//*[local-name()='manufacturer']")
            if ($mk) { $svc.Make = $mk.InnerText.Trim() }
            $md = $x.SelectSingleNode("//*[local-name()='modelName']")
            if ($md) { $svc.Model = $md.InnerText.Trim() }
            if ($type -match "WANIPConnection") { return $svc }
            if (-not $best) { $best = $svc }
        }
    }
    return $best
}

function Invoke-Igd($svc, [string]$action, [string]$argXml, [int]$timeoutMs = 5000) {
    $body = '<?xml version="1.0"?>' +
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">' +
            "<s:Body><u:$action xmlns:u=`"$($svc.Type)`">$argXml</u:$action></s:Body></s:Envelope>"
    $r = Invoke-Http $svc.Ctl $body "$($svc.Type)#$action" $timeoutMs
    if ($r.Code -eq 200) { return @{ Ok = $true; Code = 0; Text = $r.Text; Desc = "" } }
    $code = $r.Code
    $desc = ""
    if ($r.Text -match "<(?:\w+:)?errorCode>\s*(\d+)") { $code = [int]$Matches[1] }
    if ($r.Text -match "<(?:\w+:)?errorDescription>([^<]*)") { $desc = $Matches[1] }
    if ($code -eq -1) { $desc = $r.Text }
    return @{ Ok = $false; Code = $code; Text = $r.Text; Desc = $desc }
}

function Get-ExternalIp($svc) {
    $r = Invoke-Igd $svc "GetExternalIPAddress" ""
    if ($r.Ok -and $r.Text -match "<(?:\w+:)?NewExternalIPAddress>\s*([^<\s]*)") { return $Matches[1] }
    return ""
}

function Get-Mapping($svc, [int]$port) {
    $r = Invoke-Igd $svc "GetSpecificPortMappingEntry" "<NewRemoteHost></NewRemoteHost><NewExternalPort>$port</NewExternalPort><NewProtocol>UDP</NewProtocol>"
    if (-not $r.Ok) { return $null }
    $client = if ($r.Text -match "<(?:\w+:)?NewInternalClient>\s*([^<\s]*)") { $Matches[1] } else { "" }
    $desc = if ($r.Text -match "<(?:\w+:)?NewPortMappingDescription>([^<]*)") { $Matches[1] } else { "" }
    return @{ Client = $client; Desc = $desc }
}

function Remove-Mapping($svc, [int]$port, [int]$timeoutMs = 5000) {
    return Invoke-Igd $svc "DeletePortMapping" "<NewRemoteHost></NewRemoteHost><NewExternalPort>$port</NewExternalPort><NewProtocol>UDP</NewProtocol>" $timeoutMs
}

function Add-Mapping($svc, [int]$port, [string]$client) {
    $lease = $LeaseS
    for ($try = 0; $try -lt 3; $try++) {
        $a = "<NewRemoteHost></NewRemoteHost><NewExternalPort>$port</NewExternalPort><NewProtocol>UDP</NewProtocol>" +
             "<NewInternalPort>$port</NewInternalPort><NewInternalClient>$client</NewInternalClient><NewEnabled>1</NewEnabled>" +
             "<NewPortMappingDescription>$MapDesc</NewPortMappingDescription><NewLeaseDuration>$lease</NewLeaseDuration>"
        $r = Invoke-Igd $svc "AddPortMapping" $a
        if ($r.Ok) {
            # Read the mapping back. Some routers return success but point the forward at the PC that asked
            # (UPnP secure mode), or keep no entry. Green only if it landed on $client.
            $m = Get-Mapping $svc $port
            if ($m -and $m.Client -eq $client) {
                $r.Verified = $true
                $r.Actual = $m.Client
                $r.Rewrote = $false
                return $r
            }
            if ($m -and $m.Client -and $m.Client -ne $client) {
                [void](Remove-Mapping $svc $port)   # wrong host: do not leave a forward that looks like ours
                return @{ Ok = $false; Code = 0; Text = ""; Verified = $false; Rewrote = $true; Actual = $m.Client;
                          Desc = "router pointed the forward at $($m.Client) instead of $client" }
            }
            [void](Remove-Mapping $svc $port)   # unconfirmed: do not leave a forward the window reports as not there
            return @{ Ok = $false; Code = 0; Text = $r.Text; Verified = $false; Rewrote = $false; Actual = "";
                      Desc = "router accepted the request but would not confirm where the port points" }
        }
        if ($r.Code -eq 725 -and $lease -ne 0) { $lease = 0; continue }      # router only takes permanent forwards
        if ($r.Code -eq 718) {                                                # this port is already forwarded
            $m = Get-Mapping $svc $port
            if ($m -and ($m.Client -eq $client -or $m.Desc -like "SCCT*")) {
                [void](Remove-Mapping $svc $port)                             # ours (or an old run of ours): replace it
                continue
            }
            if ($m) { $r.Desc = "already forwarded to $($m.Client) ($($m.Desc))" }
        }
        return $r
    }
    return $r
}

function Explain-Code([int]$code, [string]$desc) {
    switch ($code) {
        -1  { return "no answer from the router ($desc)" }
        401 { return "the router does not offer port forwarding through UPnP (401)" }
        501 { return "the router refused the request (501 Action Failed)" }
        606 { return "the router does not allow UPnP changes (606 Action not authorized). UPnP may be set to read-only." }
        715 { return "the router does not accept forwards from any address (715)" }
        716 { return "the router needs a different external port setting (716)" }
        718 { return "UDP port already forwarded to another device: $desc" }
        724 { return "the router only accepts the same external and internal port (724)" }
        729 { return "the router refused: conflict with another forwarding setting (729)" }
        0   { if ($desc) { return $desc }; return "the router did not confirm the forward" }
        default { return "router error $code $desc".Trim() }
    }
}

function Invoke-MapAll($svc, [string]$client) {
    $ok = @(); $bad = @(); $actual = ""
    foreach ($p in $GamePorts) {
        $r = Add-Mapping $svc $p $client
        if ($r.Ok -and $r.Verified) { $ok += $p }
        else {
            $bad += "UDP ${p}: " + (Explain-Code $r.Code $r.Desc)
            if ($r.Actual -and -not $actual) { $actual = [string]$r.Actual }
        }
    }
    return @{ Ok = $ok; Bad = $bad; Actual = $actual }
}
'@

. ([scriptblock]::Create($script:UpnpLib))

$script:ScopsServer = "38.49.212.120"
$script:RelayPort = 45000
$script:OpenFlag = $false

function Send-OpenFlag {
    # Hint only. The relay marks this PC's public address open for 30 minutes.
    # Sent only when the router's address is public; private and CGNAT stay quiet.
    try {
        $udp = New-Object System.Net.Sockets.UdpClient
        $bytes = [System.Text.Encoding]::ASCII.GetBytes('{"op":"upnp_open"}')
        [void]$udp.Send($bytes, $bytes.Length, $script:ScopsServer, $script:RelayPort)
        $udp.Close()
        return $true
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------- router test (V7)
# After a green check the window forwards UDP 3659 to THIS PC for about a minute and asks the SCOPS server to send test
# packets to it: two from server ports this network has never talked to (fresh), and one, 45 s later, from a port this
# PC has just answered on (stale). It shows what arrived and tells the server. Same router and the same kind of forward as
# the PS2's, without the game or a map load in the way. The server only logs the result for now.
$script:TestPort = 3659
$script:TestRule = "scopsDNS-router-test-UDP"
$script:Probe = $null

function Test-FirewallRule {
    try {
        $null = & netsh advfirewall firewall show rule name=$($script:TestRule) 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Add-FirewallRule {
    $a = "advfirewall firewall add rule name=$($script:TestRule) dir=in action=allow protocol=UDP " +
         "localport=$($script:TestPort) profile=private,domain"
    try {
        $p = Start-Process -FilePath "netsh.exe" -ArgumentList $a -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        return ($p.ExitCode -eq 0)
    } catch { return $false }      # the user said no to the Windows prompt
}

# V8.16: PCSX2 Sockets receives the game on this PC, so Windows has to allow the same UDP ports the router forwards.
# Same rule name scopsDNS.py --close-firewall already deletes (scopsDNS-PCSX2-game-UDP).
$script:GameRule = "scopsDNS-PCSX2-game-UDP"

function Test-SocketsFirewall {
    try {
        $null = & netsh advfirewall firewall show rule "name=$($script:GameRule)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Add-SocketsFirewall {
    if (Test-SocketsFirewall) { return $true }
    $a = "advfirewall firewall add rule name=$($script:GameRule) dir=in action=allow protocol=UDP " +
         "localport=3658,10070-10080 profile=private,domain"
    try {
        $p = Start-Process -FilePath "netsh.exe" -ArgumentList $a -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        return ($p.ExitCode -eq 0)
    } catch { return $false }
}

function Send-ProbeMsg([string]$json, $dest = $null) {
    $b = [System.Text.Encoding]::ASCII.GetBytes($json)
    try {
        if ($dest) { [void]$script:Probe.Sock.Send($b, $b.Length, $dest) }
        else { [void]$script:Probe.Sock.Send($b, $b.Length, $script:ScopsServer, $script:RelayPort) }
    } catch {}
}

function Start-RouterTest($svc) {
    Stop-RouterTest
    $pc = (@(Get-LanCandidates))[0].Ip
    if (-not $pc) { return }
    $detail.AppendText("`r`n`r`nRouter test: checking that new connections from the internet reach your network...")
    if (-not (Test-FirewallRule)) {
        $detail.AppendText("`r`nWindows will ask once to let this window receive the test (UDP $($script:TestPort)).")
        [System.Windows.Forms.Application]::DoEvents()
        if (-not (Add-FirewallRule)) {
            $detail.AppendText("`r`nRouter test skipped: Windows did not allow it. Hosting still works.")
            return
        }
    }
    $m = Add-Mapping $svc $script:TestPort $pc
    if (-not $m.Ok) {
        $detail.AppendText("`r`nRouter test skipped: the router would not forward the test port to this PC (" +
                           (Explain-Code $m.Code $m.Desc) + ").")
        return
    }
    try {
        $sock = New-Object System.Net.Sockets.UdpClient($script:TestPort)
        try { [void]$sock.Client.IOControl(-1744830452, [byte[]](0, 0, 0, 0), $null) } catch {}  # no reset errors on receive
    } catch {
        [void](Remove-Mapping $svc $script:TestPort 1500)
        $detail.AppendText("`r`nRouter test skipped: UDP $($script:TestPort) is already in use on this PC.")
        return
    }
    $id = -join ((1..8) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    $script:Probe = @{ Sock = $sock; Id = $id; T0 = Get-Date; Seen = @{}; Status = ""; Svc = $svc; Done = $false; StaleS = 45.0 }
    Send-ProbeMsg ('{"op":"upnp_probe","id":"' + $id + '","port":' + $script:TestPort + '}')
    $script:ProbeTimer.Start()
}

function Finish-RouterTest {
    $p = $script:Probe
    if (-not $p -or $p.Done) { return }
    $p.Done = $true
    $script:ProbeTimer.Stop()
    try { $p.Sock.Close() } catch {}
    try { [void](Remove-Mapping $p.Svc $script:TestPort 1500) } catch {}
    $f = @(@("fresh1", "fresh2") | Where-Object { $p.Seen.ContainsKey($_) }).Count
    if ($p.Status -like "recent*") { $msg = "Router test: already run in the last 10 minutes; the server keeps that result." }
    elseif ($p.Status -eq "busy") { $msg = "Router test: one is already running for your address." }
    elseif (-not $p.Status -and $f -eq 0) { $msg = "Router test: the SCOPS server did not answer (it may not have the router test yet)." }
    elseif ($p.Status -and $p.Status -ne "started") { $msg = "Router test not run (server said: $($p.Status))." }
    elseif ($f -eq 0) {
        $msg = "Router test: nothing arrived. The router did not forward the test port, or a firewall on this PC blocked it. " +
               "Hosting still works through the relay."
    } else {
        $msg = "Router test: $f of 2 new connections from the internet reached this PC" + $(if ($f -eq 2) { " - the forward works for new connections. The game's own port can still behave differently." } else { "." })
        if ($p.Seen.ContainsKey("stale")) { $msg += " A connection reused after $([int]$p.StaleS) s also arrived." }
        else { $msg += " A connection reused after $([int]$p.StaleS) s did not (common; the server notes it)." }
    }
    $detail.AppendText("`r`n$msg")
    $script:Probe = $null
}

function Stop-RouterTest { if ($script:Probe) { Finish-RouterTest } }

$script:ProbeTimer = New-Object System.Windows.Forms.Timer
$script:ProbeTimer.Interval = 250
$script:ProbeTimer.Add_Tick({
    $p = $script:Probe
    if (-not $p) { $script:ProbeTimer.Stop(); return }
    try {
        while ($p.Sock.Available -gt 0) {
            $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
            $bytes = $p.Sock.Receive([ref]$ep)
            if ($ep.Address.ToString() -ne $script:ScopsServer) { continue }
            $m = $null
            try { $m = [System.Text.Encoding]::ASCII.GetString($bytes) | ConvertFrom-Json } catch { continue }
            if ([string]$m.id -ne $p.Id) { continue }
            if ($m.op -eq "upnp_probe_reply") {
                $p.Status = [string]$m.status
                if ($m.stale_s) { $p.StaleS = [double]$m.stale_s }
                if ($p.Status -ne "started") { Finish-RouterTest; return }
            } elseif ($m.op -eq "upnp_probe_pkt") {
                $k = [string]$m.kind
                if (-not $p.Seen.ContainsKey($k)) {
                    $p.Seen[$k] = $true
                    Send-ProbeMsg ('{"op":"upnp_probe_seen","id":"' + $p.Id + '","kind":"' + $k + '"}')
                    if ($k -eq "fresh1") { Send-ProbeMsg '{"op":"probe_ack"}' $ep }   # makes that flow two-way
                }
            }
        }
    } catch {}
    $age = ((Get-Date) - $p.T0).TotalSeconds
    $fresh = @(@("fresh1", "fresh2") | Where-Object { $p.Seen.ContainsKey($_) }).Count
    if ($p.Seen.ContainsKey("stale") -or $age -gt ($p.StaleS + 25)) { Finish-RouterTest }
    elseif ($fresh -eq 0 -and $age -gt 12) { Finish-RouterTest }        # fresh tests come within 2 s of the request
})

# ---------------------------------------------------------------- the check (runs in the window's own thread; takes a few seconds)
function Invoke-Check([string]$ps2) {
    $ErrorActionPreference = "Stop"     # any unexpected error is shown in the window with its line
    $res = @{ State = "bad"; Head = ""; Lines = @(); Gateway = ""; Svc = $null; Mapped = @() }
    $cands = @(Get-LanCandidates)
    if ($cands.Count -eq 0) {
        $res.Head = "No network found"
        $res.Lines = @("This PC has no network adapter with a router (gateway). Connect to your home network and try again.")
        return $res
    }
    $res.Gateway = $cands[0].Gw
    $pc = $cands[0].Ip
    $notes = @()
    if ($ps2 -eq $pc) {
        if ($sockets -and $sockets.Checked) {
            $notes += "Sockets mode: $ps2 is this PC. That is correct for PCSX2 Sockets. The game arrives on Windows, not on a PS2 address."
        } else {
            $notes += "Note: $ps2 is this PC's own address. With PCSX2 in PCAP Bridged mode the game uses the PS2 Address " +
                      "(Settings > Network & HDD), not the PC's. Tick 'PCSX2 Sockets mode' only if PCSX2 is in Sockets mode."
        }
    }
    $vpn = @($cands | Where-Object { $_.Name -match "TAP|Wintun|WireGuard|VPN|Proton|OpenVPN|Tailscale|ZeroTier|NordLynx" })

    $found = Find-Igd $cands
    if ($found.Count -eq 0) {
        $res.Head = "UPnP not available"
        $res.Lines = @("No UPnP router answered on this network.", "",
            "What you can try:",
            "  - Open your router's settings (button below) and turn on UPnP, then check again.",
            "  - In Windows, set this network to Private and turn on Network Discovery.",
            "  - Or forward UDP 3658 and 10070-10080 to $ps2 by hand in the router.",
            "",
            "Without UPnP or a forward you can still host: the SCOPS server relay gets you online, " +
            "players may just wait longer when they reconnect at a map change.")
        if ($vpn.Count -gt 0) { $res.Lines += @("", "A VPN adapter is active ($($vpn[0].Name)). Disconnect the VPN and check again.") }
        $res.Lines += @("") + $notes
        return $res
    }

    $svc = $null; $dev = $null
    foreach ($loc in $found.Keys) {
        $svc = Get-WanService $loc
        if ($svc) { $dev = $found[$loc]; break }
    }
    if (-not $svc) {
        $res.Head = "UPnP not available"
        $res.Lines = @("A UPnP device answered, but none offers port forwarding (it may be a TV, printer or media box, " +
                       "not the router).", "", "Turn on UPnP in your router's settings, or forward UDP 3658 and 10070-10080 to $ps2 by hand.") + @("") + $notes
        return $res
    }
    $res.Gateway = $dev.Gw
    $res.Svc = $svc
    $name = if ($svc.Name) { $svc.Name } else { "your router" }
    $ext = Get-ExternalIp $svc

    $test = Add-Mapping $svc 3658 $ps2
    if (-not $test.Ok) {
        $res.State = "bad"
        $res.Head = "UPnP is on, but the router refused"
        if ($test.Rewrote) {
            $res.State = "warn"
            $res.Head = "Router rewrote the forward"
            $res.Lines = @("$name accepted UPnP but pointed UDP 3658 at $($test.Actual), not $ps2.",
                           "Some routers do this when UPnP secure mode is on: they only forward to the PC that asked, even when you type the PS2 address.", "",
                           "What to do:",
                           "  - Turn off UPnP secure mode in the router's settings, then check again (ASUS: WAN > Internet Connection > 'Enable secure UPnP IGD mode' = No).",
                           "  - Or forward UDP 3658 and 10070-10080 to $ps2 by hand. As a last resort, use the DMZ: only for a PS2 console (never a PC), and turn it off after playing.", "",
                           "The SCOPS server is not being told you are open. You can still host through the relay.") + @("") + $notes
            return $res
        }
        if ($test.Desc -match "would not confirm") {
            $res.State = "warn"
            $res.Head = "Router did not confirm the forward"
            $res.Lines = @("$name accepted the request for UDP 3658, but would not confirm that it points at $ps2.",
                           "Without that confirmation this window will not call the ports forwarded.", "",
                           "Forward UDP 3658 and 10070-10080 to $ps2 by hand. As a last resort, use the DMZ: only for a PS2 console (never a PC), and turn it off after playing, and check again.", "",
                           "You can still host through the SCOPS server relay.") + @("") + $notes
            return $res
        }
        $res.Lines = @("$name answered UPnP but would not forward UDP 3658 to ${ps2}:",
                       "  " + (Explain-Code $test.Code $test.Desc), "",
                       "Many routers only let a device open ports for itself, so a PS2 or PCSX2 address is refused. " +
                       "Look in the router's UPnP settings for a 'secure mode' or similar, or forward UDP 3658 and " +
                       "10070-10080 to $ps2 by hand.", "",
                       "You can still host through the SCOPS server relay.") + @("") + $notes
        return $res
    }

    $all = Invoke-MapAll $svc $ps2
    $res.Mapped = $all.Ok
    $need = @($GamePorts).Count
    $got = @($all.Ok).Count
    if ($got -lt $need) {
        $res.State = "warn"
        $res.Head = "Ports not confirmed on the PS2"
        $where = if ($all.Actual) { " The router pointed at least one at $($all.Actual) instead." } else { "" }
        $res.Lines = @("$name confirmed $got of $need UDP ports on $ps2.$where",
                       "Green is only shown when every game port is read back pointing at the PS2 address you typed.", "",
                       "Check that UPnP secure mode is off in the router (ASUS: WAN > Internet Connection > 'Enable secure UPnP IGD mode' = No) and check again, or forward UDP 3658 and 10070-10080 to $ps2 by hand. As a last resort, use the DMZ: only for a PS2 console (never a PC), and turn it off after playing.", "",
                       "The SCOPS server is not being told you are open. Hosting through the relay still works.")
        if ($all.Bad.Count -gt 0) { $res.Lines += @("", "Not confirmed:") + ($all.Bad | Select-Object -First 6 | ForEach-Object { "  $_" }) }
        $res.Lines += @("") + $notes
        return $res
    }
    if (Test-PrivateIp $ext) {
        $res.State = "warn"
        $res.Head = "UPnP works, but you are behind a second NAT"
        $shown = if ($ext) { $ext } else { "none reported" }
        $res.Lines = @("$name opened the ports, but its internet address is $shown, which is not a public address.",
                       "Either another router sits in front of this one, or your internet provider shares one address " +
                       "between customers (CGNAT). Players on the internet still cannot reach $ps2 directly.", "",
                       "  - If you have a modem-router plus your own router, turn on UPnP (or a forward) on the modem too, " +
                       "or put the modem in bridge mode.",
                       "  - If it is CGNAT, only your provider can change it. The SCOPS server relay still gets you online.")
    } else {
        $res.State = "ok"
        $res.Head = "Ports forwarded"
        $dnsNext = "Next: leave this window open while you host."
        if ($script:DnsState -eq "ok") {
            $dnsNext = "Next: leave this window open. Put 38.49.212.120 in both DNS entries on the console (or in PCSX2)."
        } elseif ($script:DnsMode -eq "serve") {
            $local = Get-PickedIp
            if ($local) { $dnsNext = "Next: leave this window open. Put $local in both DNS entries (this PC is the local DNS)." }
        }
        $who = if ($script:HostMode -eq "ps3") { "PS3" } elseif ($script:HostMode -eq "sockets") { "this PC" } else { "the console" }
        $res.Lines = @("$name confirmed $($all.Ok.Count) of $($GamePorts.Count) UDP ports on $ps2 (read back from the router).",
                       "Public address: $ext", "",
                       $dnsNext,
                       "Forwards point at $who. They renew every 20 minutes and are removed when you close this window.",
                       "This can help players get back in faster on some routers. If it does not on yours, the SCOPS " +
                       "server still gets your players to you.",
                       "Players joining: have each one wait 20-30 seconds after the last one got in.")
        if ("$name $($svc.Make) $($svc.Model)" -match "ASUS" -or ($script:Brand -and $script:Brand.Name -eq "ASUS")) {
            $res.Lines += @("", "ASUS: in testing, a hand-made forward kept players' paths open where UPnP did not. " +
                           "To set one up, close this window first (that removes its UPnP forwards), then press Open my router.")
        }
    }
    if ($all.Bad.Count -gt 0) { $res.Lines += @("", "Not forwarded:") + ($all.Bad | ForEach-Object { "  $_" }) }
    $res.Lines += @("") + $notes
    return $res
}

# ---------------------------------------------------------------- the window
$form = New-Object System.Windows.Forms.Form
$form.Text = "SCOPS V8.18 - Splinter Cell Chaos Theory"
$form.ClientSize = New-Object System.Drawing.Size(560, 640)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)

$dnsTitle = New-Object System.Windows.Forms.Label
$dnsTitle.Text = "DNS"
$dnsTitle.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$dnsTitle.Location = New-Object System.Drawing.Point(16, 10)
$dnsTitle.AutoSize = $true
$form.Controls.Add($dnsTitle)

$script:DnsState = "busy"
$dnsIcon = New-Object System.Windows.Forms.Panel
$dnsIcon.Location = New-Object System.Drawing.Point(16, 40)
$dnsIcon.Size = New-Object System.Drawing.Size(64, 64)
$form.Controls.Add($dnsIcon)
$dnsIcon.Add_Paint({
    param($s, $e)
    $g = $e.Graphics
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $col = switch ($script:DnsState) {
        "ok"   { [System.Drawing.Color]::FromArgb(46, 160, 67) }
        "bad"  { [System.Drawing.Color]::FromArgb(207, 34, 46) }
        "warn" { [System.Drawing.Color]::FromArgb(222, 150, 0) }
        default { [System.Drawing.Color]::FromArgb(160, 160, 160) }
    }
    $brush = New-Object System.Drawing.SolidBrush($col)
    $g.FillEllipse($brush, 2, 2, 58, 58)
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 7)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    switch ($script:DnsState) {
        "ok" {
            $pts = [System.Drawing.Point[]]@([System.Drawing.Point]::new(17, 32), [System.Drawing.Point]::new(27, 42),
                                             [System.Drawing.Point]::new(45, 21))
            $g.DrawLines($pen, $pts)
        }
        "bad" { $g.DrawLine($pen, 20, 20, 42, 42); $g.DrawLine($pen, 42, 20, 20, 42) }
        "warn" { $g.DrawLine($pen, 31, 15, 31, 34); $g.FillEllipse($white, 27, 41, 9, 9) }
        default { foreach ($x in 15, 27, 39) { $g.FillEllipse($white, $x, 27, 8, 8) } }
    }
    $pen.Dispose(); $brush.Dispose(); $white.Dispose()
})

$dnsHead = New-Object System.Windows.Forms.Label
$dnsHead.Text = "Checking DNS..."
$dnsHead.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$dnsHead.Location = New-Object System.Drawing.Point(92, 48)
$dnsHead.Size = New-Object System.Drawing.Size(468, 26)
$form.Controls.Add($dnsHead)

$dnsAddr = New-Object System.Windows.Forms.Label
$dnsAddr.Text = "PS2 DNS: waiting..."
$dnsAddr.Font = New-Object System.Drawing.Font("Segoe UI", 11)
$dnsAddr.Location = New-Object System.Drawing.Point(92, 74)
$dnsAddr.Size = New-Object System.Drawing.Size(468, 24)
$form.Controls.Add($dnsAddr)

$dnsDetail = New-Object System.Windows.Forms.Label
$dnsDetail.Text = "Checking whether your provider rewrites DNS."
$dnsDetail.ForeColor = [System.Drawing.Color]::DimGray
$dnsDetail.Location = New-Object System.Drawing.Point(92, 98)
$dnsDetail.Size = New-Object System.Drawing.Size(320, 40)
$form.Controls.Add($dnsDetail)

$dnsBtn = New-Object System.Windows.Forms.Button
$dnsBtn.Text = "Use local DNS"
$dnsBtn.Location = New-Object System.Drawing.Point(420, 96)
$dnsBtn.Size = New-Object System.Drawing.Size(124, 30)
$dnsBtn.Visible = $false
$form.Controls.Add($dnsBtn)

$dnsRetry = New-Object System.Windows.Forms.Button
$dnsRetry.Text = "Check again"
$dnsRetry.Location = New-Object System.Drawing.Point(420, 96)
$dnsRetry.Size = New-Object System.Drawing.Size(124, 30)
$dnsRetry.Visible = $false
$form.Controls.Add($dnsRetry)
$script:DnsTries = 0

$title = New-Object System.Windows.Forms.Label
$title.Text = "Hosting (UPnP)"
$title.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(16, 140)
$title.AutoSize = $true
$form.Controls.Add($title)

$lbl = New-Object System.Windows.Forms.Label
$lbl.Text = "PS2 address:"
$lbl.Location = New-Object System.Drawing.Point(16, 176)
$lbl.AutoSize = $true
$form.Controls.Add($lbl)

$box = New-Object System.Windows.Forms.TextBox
$box.Location = New-Object System.Drawing.Point(110, 173)
$box.Size = New-Object System.Drawing.Size(128, 26)
$form.Controls.Add($box)

$copyAddr = New-Object System.Windows.Forms.Button
$copyAddr.Text = "Copy"
$copyAddr.Location = New-Object System.Drawing.Point(242, 173)
$copyAddr.Size = New-Object System.Drawing.Size(46, 26)
$form.Controls.Add($copyAddr)
$copyAddr.Add_Click({
    $t = $box.Text.Trim()
    if ($t) { try { [System.Windows.Forms.Clipboard]::SetText($t) } catch {} }
})

$btn = New-Object System.Windows.Forms.Button
$btn.Text = "Check UPnP"
$btn.Location = New-Object System.Drawing.Point(292, 173)
$btn.Size = New-Object System.Drawing.Size(120, 30)
$form.Controls.Add($btn)
$form.AcceptButton = $btn

$hint = New-Object System.Windows.Forms.Label
$hint.Text = "Pick what you host with. Find fills a console address. Sockets uses this PC. PS3 is the launch model only."
$hint.UseMnemonic = $false
$hint.ForeColor = [System.Drawing.Color]::DimGray
$hint.Location = New-Object System.Drawing.Point(16, 208)
$hint.Size = New-Object System.Drawing.Size(530, 36)
$form.Controls.Add($hint)

$script:State = "idle"
$icon = New-Object System.Windows.Forms.Panel
$icon.Location = New-Object System.Drawing.Point(16, 252)
$icon.Size = New-Object System.Drawing.Size(64, 64)
$form.Controls.Add($icon)
$icon.Add_Paint({
    param($s, $e)
    $g = $e.Graphics
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $col = switch ($script:State) {
        "ok"   { [System.Drawing.Color]::FromArgb(46, 160, 67) }
        "bad"  { [System.Drawing.Color]::FromArgb(207, 34, 46) }
        "warn" { [System.Drawing.Color]::FromArgb(222, 150, 0) }
        default { [System.Drawing.Color]::FromArgb(160, 160, 160) }
    }
    $brush = New-Object System.Drawing.SolidBrush($col)
    $g.FillEllipse($brush, 2, 2, 58, 58)
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::White, 7)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    switch ($script:State) {
        "ok" {
            $pts = [System.Drawing.Point[]]@([System.Drawing.Point]::new(17, 32), [System.Drawing.Point]::new(27, 42),
                                             [System.Drawing.Point]::new(45, 21))
            $g.DrawLines($pen, $pts)
        }
        "bad" { $g.DrawLine($pen, 20, 20, 42, 42); $g.DrawLine($pen, 42, 20, 20, 42) }
        "warn" { $g.DrawLine($pen, 31, 15, 31, 34); $g.FillEllipse($white, 27, 41, 9, 9) }
        "busy" { foreach ($x in 15, 27, 39) { $g.FillEllipse($white, $x, 27, 8, 8) } }
        default {
            $f = New-Object System.Drawing.Font("Segoe UI", 22, [System.Drawing.FontStyle]::Bold)
            $g.DrawString("?", $f, $white, 18, 9)
        }
    }
    $pen.Dispose(); $brush.Dispose(); $white.Dispose()
})

$head = New-Object System.Windows.Forms.Label
$head.Text = "Pick what you host with, then Check UPnP."
$head.Font = New-Object System.Drawing.Font("Segoe UI", 13, [System.Drawing.FontStyle]::Bold)
$head.Location = New-Object System.Drawing.Point(92, 268)
$head.Size = New-Object System.Drawing.Size(455, 34)
$form.Controls.Add($head)

$detail = New-Object System.Windows.Forms.TextBox
$detail.Multiline = $true
$detail.ReadOnly = $true
$detail.ScrollBars = "Vertical"
$detail.BackColor = [System.Drawing.SystemColors]::Window
$detail.Location = New-Object System.Drawing.Point(16, 328)
$detail.Size = New-Object System.Drawing.Size(528, 206)
$form.Controls.Add($detail)

$router = New-Object System.Windows.Forms.Button
$router.Text = "Open my router"
$router.Location = New-Object System.Drawing.Point(16, 572)
$router.Size = New-Object System.Drawing.Size(140, 32)
$router.Visible = $false
$form.Controls.Add($router)

$routerNote = New-Object System.Windows.Forms.Label
$routerNote.Text = "Best if you can: forward the ports by hand. You need your router's login."
$routerNote.UseMnemonic = $false
$routerNote.ForeColor = [System.Drawing.Color]::DimGray
$routerNote.Location = New-Object System.Drawing.Point(16, 540)
$routerNote.Size = New-Object System.Drawing.Size(528, 30)
$routerNote.Visible = $false
$form.Controls.Add($routerNote)

$guide = New-Object System.Windows.Forms.Button
$guide.Text = "Guide for my router"
$guide.Location = New-Object System.Drawing.Point(162, 572)
$guide.Size = New-Object System.Drawing.Size(160, 32)
$guide.Visible = $false
$form.Controls.Add($guide)
$guide.Add_Click({
    $b = Find-RouterBrand
    try { Start-Process $b.Url } catch { $detail.AppendText("`r`n`r`nOpen this in your browser: $($b.Url)") }
})

$close = New-Object System.Windows.Forms.Button
$close.Text = "Close"
$close.Location = New-Object System.Drawing.Point(444, 572)
$close.Size = New-Object System.Drawing.Size(100, 32)
$form.Controls.Add($close)
$close.Add_Click({ $form.Close() })

# ---- Log window (V8.14): a "Log" button opens a window with the live log of this SCOPS window (idea from PSRewired's
# RetroDNS Log tab). It shows what this window found plus the last lines of its two logs, refreshes every 2 s, and
# "Copy all" puts it on the clipboard to paste in Discord. It reads only local files; it sends nothing anywhere.
# Public addresses are cut to their first two parts (the SCOPS server and the test address stay whole), and the
# Windows user name in paths becomes %USERNAME%, so the text is safe to share.
$report = New-Object System.Windows.Forms.Button
$report.Text = "Log"
$report.Location = New-Object System.Drawing.Point(330, 572)
$report.Size = New-Object System.Drawing.Size(108, 32)
$form.Controls.Add($report)
$tip = New-Object System.Windows.Forms.ToolTip
$tip.SetToolTip($report, "Shows the log. Use Copy all and paste it in Discord when something does not work.")

function Hide-Private([string]$text) {
    if (-not $text) { return "" }
    $keep = @("38.49.212.120", "192.0.2.1", "8.8.8.8", "1.1.1.1", "0.0.0.0", "255.255.255.255")
    $text = [regex]::Replace($text, '\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b', [System.Text.RegularExpressions.MatchEvaluator]{
        param($m)
        $ip = $m.Value
        if ($keep -contains $ip) { return $ip }
        $a = [int]$m.Groups[1].Value; $b = [int]$m.Groups[2].Value
        if ($a -gt 255 -or $b -gt 255 -or [int]$m.Groups[3].Value -gt 255 -or [int]$m.Groups[4].Value -gt 255) { return $ip }
        if ($a -eq 10 -or $a -eq 127 -or ($a -eq 192 -and $b -eq 168) -or ($a -eq 172 -and $b -ge 16 -and $b -le 31) -or
            ($a -eq 169 -and $b -eq 254) -or ($a -eq 100 -and $b -ge 64 -and $b -le 127) -or $a -ge 224) { return $ip }
        return "$a.$b.x.x"
    })
    if ($env:USERNAME) { $text = $text -replace [regex]::Escape("\Users\$($env:USERNAME)"), "\Users\%USERNAME%" }
    return $text
}

function Read-Tail([string]$path, [int]$max, [string]$since = $null) {
    if (-not $path -or -not (Test-Path $path)) { return @("(no file)") }
    try {
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $sr = New-Object System.IO.StreamReader($fs)
        $all = @($sr.ReadToEnd() -split "`r?`n")
        $sr.Close()
    } catch { return @("(could not read: $($_.Exception.Message))") }
    if ($since) {
        $start = 0
        for ($i = $all.Count - 1; $i -ge 0; $i--) { if ($all[$i] -like "*$since*") { $start = $i; break } }
        $all = @($all[$start..($all.Count - 1)])
    }
    $all = @($all | Where-Object { $_ -ne "" })
    if ($all.Count -eq 0) { return @("(empty)") }
    if ($all.Count -gt $max) { $all = @("... ($($all.Count - $max) earlier lines left out)") + @($all[($all.Count - $max)..($all.Count - 1)]) }
    return $all
}

function Get-ScopsReport {
    $dir = Split-Path -Parent $PSCommandPath
    if (-not $script:OsText) {
        try { $o = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop; $script:OsText = "$($o.Caption) $($o.Version)" } catch { $script:OsText = [Environment]::OSVersion.VersionString }
    }
    $os = $script:OsText
    $mode = "scopsDNS.py (Python)"
    if (Test-Path (Join-Path $dir "scopsDNS.exe")) { $mode = "scopsDNS.exe" }
    if ($script:DnsToolKind) { $mode = $script:DnsToolKind }
    if ($script:DnsExeOk -eq $false) { $mode += " (scopsDNS.exe here is an old build, skipped)" }
    $alive = "not started"
    if ($script:DnsProc) {
        if (-not $script:DnsProc.HasExited) { $alive = "running" }
        elseif ($script:DnsMode -eq "check") { $alive = "check finished (exit $($script:DnsProc.ExitCode))" }
        else { $alive = "stopped (exit $($script:DnsProc.ExitCode))" }
    }
    if (-not $script:FwAt -or ((Get-Date) - $script:FwAt).TotalSeconds -gt 15) {   # netsh is slow: ask at most every 15 s
        $script:FwText = "unknown"
        try { if (Test-FirewallRule) { $script:FwText = "yes" } else { $script:FwText = "no" } } catch {}
        $script:FwAt = Get-Date
    }
    $fw = $script:FwText
    $routerName = ""
    if ($script:Brand) { $routerName = "$($script:Brand.Name) $($script:Brand.Model)".Trim() } elseif ($script:RouterInfo) { $routerName = $script:RouterInfo }
    $L = New-Object System.Collections.Generic.List[string]
    $L.Add("=== SCOPS log (V8.18) ===")
    $L.Add("Time:      " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
    $L.Add("Windows:   $os, PowerShell $($PSVersionTable.PSVersion)")
    $L.Add("DNS tool:  $mode, $alive, mode=$($script:DnsMode), tries=$($script:DnsTries)")
    $L.Add("")
    $L.Add("--- DNS ---")
    $L.Add("Result:    [$($script:DnsState)] $($dnsHead.Text)")
    $L.Add("           $($dnsAddr.Text)")
    $L.Add("           $($dnsDetail.Text)")
    $L.Add("")
    $L.Add("--- Network cards (picked: $($nicBox.Text)) ---")
    foreach ($a in @($script:PcAdapters)) {
        $gwTxt = "no"; if ($a.HasGw) { $gwTxt = "yes" }
        $L.Add("  $($a.Ip)  $($a.Kind)  router=$gwTxt  [$($a.Name)]")
    }
    if (@($script:PcAdapters).Count -eq 0) { $L.Add("  none found") }
    $L.Add("")
    $L.Add("--- Hosting (UPnP) ---")
    $L.Add("PS2 address: $($box.Text.Trim())")
    $sockTxt = "no"
    if ($sockets -and $sockets.Checked) { $sockTxt = "yes" }
    $L.Add("Sockets:    $sockTxt")
    $L.Add("Host mode:  $($script:HostMode)")
    $L.Add("Result:    $($head.Text)")
    $L.Add("Router:    $($script:Gateway)  $routerName")
    $L.Add("Forwards held by this window: $(@($script:Mapped).Count)   Firewall test rule present: $fw")
    if ($detail.Text.Trim()) { foreach ($d in ($detail.Text -split "`r?`n")) { $L.Add("  | $d") } }
    $L.Add("")
    $L.Add("--- scops-dns.log (last 60 lines) ---")
    foreach ($x in (Read-Tail (Join-Path $dir "scops-dns.log") 60)) { $L.Add($x) }
    $L.Add("")
    $L.Add("--- scops-window.log (this window, last 80 lines) ---")
    foreach ($x in (Read-Tail $script:LogFile 80 "---- window opened ----")) { $L.Add($x) }
    $L.Add("=== end of log ===")
    return (Hide-Private ($L -join "`r`n"))
}

$script:LogWin = $null
function Update-LogWindow([bool]$force = $false) {
    if (-not $script:LogWin -or $script:LogWin.IsDisposed) { return }
    $tb = $script:LogText
    if (-not $force -and $tb.SelectionLength -gt 0) { return }          # the player is selecting text: do not jump
    $text = ""
    try { $text = Get-ScopsReport } catch { $text = "Log failed: $($_.Exception.Message)" }
    if ($text -eq $tb.Text) { return }
    $atEnd = ($tb.SelectionStart -ge ($tb.TextLength - 2)) -or $force
    $keep = $tb.SelectionStart
    $tb.Text = $text
    if ($atEnd) { $tb.SelectionStart = $tb.TextLength; $tb.ScrollToCaret() }
    else { $tb.SelectionStart = [Math]::Min($keep, $tb.TextLength); $tb.ScrollToCaret() }
}

function Show-LogWindow {
    if ($script:LogWin -and -not $script:LogWin.IsDisposed) {
        if ($script:LogWin.WindowState -eq "Minimized") { $script:LogWin.WindowState = "Normal" }
        $script:LogWin.Activate(); Update-LogWindow $true; return
    }
    $w = New-Object System.Windows.Forms.Form
    $w.Text = "SCOPS log"
    $w.ClientSize = New-Object System.Drawing.Size(760, 520)
    $w.MinimumSize = New-Object System.Drawing.Size(480, 300)
    $w.StartPosition = "CenterParent"
    $w.Font = New-Object System.Drawing.Font("Segoe UI", 9.5)
    $w.ShowInTaskbar = $true

    $tb = New-Object System.Windows.Forms.TextBox
    $tb.Multiline = $true; $tb.ReadOnly = $true; $tb.WordWrap = $false
    $tb.ScrollBars = "Both"
    $tb.BackColor = [System.Drawing.Color]::FromArgb(24, 24, 32)
    $tb.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 230)
    $tb.Font = New-Object System.Drawing.Font("Consolas", 9)
    $tb.Location = New-Object System.Drawing.Point(10, 10)
    $tb.Size = New-Object System.Drawing.Size(740, 456)
    $tb.Anchor = "Top, Bottom, Left, Right"
    $w.Controls.Add($tb)

    $note = New-Object System.Windows.Forms.Label
    $note.Text = "Updates every 2 s. Public address shortened, Windows user name hidden."
    $note.ForeColor = [System.Drawing.Color]::DimGray
    $note.Location = New-Object System.Drawing.Point(10, 482)
    $note.Size = New-Object System.Drawing.Size(330, 30)
    $note.Anchor = "Bottom, Left"
    $w.Controls.Add($note)

    $copy = New-Object System.Windows.Forms.Button
    $copy.Text = "Copy all"
    $copy.Location = New-Object System.Drawing.Point(430, 476)
    $copy.Size = New-Object System.Drawing.Size(100, 32)
    $copy.Anchor = "Bottom, Right"
    $w.Controls.Add($copy)

    $folder = New-Object System.Windows.Forms.Button
    $folder.Text = "Open folder"
    $folder.Location = New-Object System.Drawing.Point(540, 476)
    $folder.Size = New-Object System.Drawing.Size(100, 32)
    $folder.Anchor = "Bottom, Right"
    $w.Controls.Add($folder)

    $shut = New-Object System.Windows.Forms.Button
    $shut.Text = "Close"
    $shut.Location = New-Object System.Drawing.Point(650, 476)
    $shut.Size = New-Object System.Drawing.Size(100, 32)
    $shut.Anchor = "Bottom, Right"
    $w.Controls.Add($shut)
    $w.CancelButton = $shut

    $script:LogWin = $w
    $script:LogText = $tb
    $script:LogCopy = $copy

    $copy.Add_Click({
        Update-LogWindow $true
        $text = $script:LogText.Text
        $file = Join-Path (Split-Path -Parent $PSCommandPath) "scops-report.txt"
        try { [System.IO.File]::WriteAllText($file, $text, (New-Object System.Text.UTF8Encoding($false))) } catch {}
        $ok = $false
        for ($t = 0; $t -lt 3 -and -not $ok; $t++) {
            try { [System.Windows.Forms.Clipboard]::SetText($text); $ok = $true } catch { Start-Sleep -Milliseconds 150 }
        }
        Write-ScopsLog "log copied ($($text.Length) chars, clipboard=$ok)"
        if ($ok) {
            $script:LogCopy.Text = "Copied!"
            $back = New-Object System.Windows.Forms.Timer
            $back.Interval = 2500
            $back.Add_Tick({ if (-not $script:LogCopy.IsDisposed) { $script:LogCopy.Text = "Copy all" }; $this.Stop(); $this.Dispose() })
            $back.Start()
        } else {
            $script:LogText.SelectAll(); $script:LogText.Focus()
            [void][System.Windows.Forms.MessageBox]::Show("Could not copy by itself. The log is selected: press Ctrl+C. It is also saved as scops-report.txt.",
                "SCOPS log", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
        }
    })
    $folder.Add_Click({ try { Start-Process explorer.exe "`"$(Split-Path -Parent $PSCommandPath)`"" } catch {} })
    $shut.Add_Click({ $script:LogWin.Close() })

    $script:LogTimer = New-Object System.Windows.Forms.Timer
    $script:LogTimer.Interval = 2000
    $script:LogTimer.Add_Tick({ try { Update-LogWindow } catch {} })
    $w.Add_FormClosed({ try { $script:LogTimer.Stop(); $script:LogTimer.Dispose() } catch {}; $script:LogWin = $null })

    Write-ScopsLog "log window opened"
    Update-LogWindow $true
    $w.Show($form)
    $script:LogTimer.Start()
}

$report.Add_Click({ try { Show-LogWindow } catch { Write-ScopsLog "log window failed: $($_.Exception.Message)" } })

# ---- PC network card (V8.12): which of this PC's addresses the PS2 should use for local DNS ----
foreach ($ctl in @($form.Controls)) { if ($ctl.Top -ge 140) { $ctl.Top += 32 } }
$form.ClientSize = New-Object System.Drawing.Size($form.ClientSize.Width, ($form.ClientSize.Height + 32))
$nicLbl = New-Object System.Windows.Forms.Label
$nicLbl.Text = "PC network card:"
$nicLbl.Location = New-Object System.Drawing.Point(92, 145)
$nicLbl.Size = New-Object System.Drawing.Size(110, 20)
$form.Controls.Add($nicLbl)
$nicBox = New-Object System.Windows.Forms.ComboBox
$nicBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$nicBox.Location = New-Object System.Drawing.Point(204, 141)
$nicBox.Size = New-Object System.Drawing.Size(340, 24)
$form.Controls.Add($nicBox)
$script:PcAdapters = @()
# @(): with one network card PowerShell 5.1 returns a single object with no .Count, and the Sockets box said
# "No network card" (V8.16, 2026-11-21). Always keep a list.
try { $script:PcAdapters = @(Get-PcAdapters) } catch { Write-ScopsLog "Get-PcAdapters failed: $($_.Exception.Message)" }
foreach ($a in $script:PcAdapters) {
    $t = "$($a.Ip)  -  $($a.Kind): $($a.Name)"
    if ($a.Virtual) { $t += "  (VPN/virtual: the PS2 cannot use this)" }
    elseif (-not $a.HasGw) { $t += "  (no router on this card)" }
    [void]$nicBox.Items.Add($t)
}
if ($nicBox.Items.Count -gt 0) { $nicBox.SelectedIndex = 0 } else { [void]$nicBox.Items.Add("No network card found"); $nicBox.SelectedIndex = 0; $nicBox.Enabled = $false }
Write-ScopsLog ("network cards: " + (($script:PcAdapters | ForEach-Object { "$($_.Ip) $($_.Kind) [$($_.Name)]" }) -join "; "))

function Get-PickedIp {
    $i = $nicBox.SelectedIndex
    if ($i -ge 0 -and $i -lt $script:PcAdapters.Count) { return $script:PcAdapters[$i].Ip }
    return $null
}

$nicBox.Add_SelectedIndexChanged({
    $i = $nicBox.SelectedIndex
    if ($i -lt 0 -or $i -ge $script:PcAdapters.Count) { return }
    $a = $script:PcAdapters[$i]
    Write-ScopsLog "network card picked: $($a.Ip) $($a.Kind) [$($a.Name)]"
    if ($script:DnsMode -eq "serve" -and $script:DnsProc -and -not $script:DnsProc.HasExited) {
        # local DNS listens on every card, so no restart is needed: just show the picked address
        $dnsAddr.Text = "Put this in the PS2, both DNS entries:  $($a.Ip)"
    }
    if ($a.Virtual) {
        $dnsDetail.Text = "That is a VPN or virtual adapter. A PS2 or PCSX2 on your home network cannot reach it. Pick your Wi-Fi or Ethernet card."
    } elseif (-not $a.HasGw) {
        $dnsDetail.Text = "This card has no router. Use it only if the PS2 is plugged into it directly."
    }
    if ($sockets -and $sockets.Checked) { $box.Text = $a.Ip }
})

# V8.16: PCSX2 Sockets. The game uses this PC's address, so the box locks to the picked network card.
foreach ($ctl in @($form.Controls)) { if ($ctl.Top -ge 230) { $ctl.Top += 32 } }
$form.ClientSize = New-Object System.Drawing.Size($form.ClientSize.Width, ($form.ClientSize.Height + 32))
$modeLbl = New-Object System.Windows.Forms.Label
$modeLbl.Text = "Hosting with:"
$modeLbl.Location = New-Object System.Drawing.Point(16, 238)
$modeLbl.AutoSize = $true
$form.Controls.Add($modeLbl)
$modeBox = New-Object System.Windows.Forms.ComboBox
$modeBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
$modeBox.Location = New-Object System.Drawing.Point(110, 234)
$modeBox.Size = New-Object System.Drawing.Size(290, 26)
[void]$modeBox.Items.Add("Real PS2")
[void]$modeBox.Items.Add("PCSX2 PCAP Bridged")
[void]$modeBox.Items.Add("PCSX2 Sockets")
[void]$modeBox.Items.Add("PS3 with PS2 support (untested)")
$modeBox.SelectedIndex = 0
$form.Controls.Add($modeBox)
$tip.SetToolTip($modeBox, "Real PS2 or PCAP Bridged: Find looks for a PS2 adapter. Sockets: this PC. PS3: only early models play PS2 games (CECHA/CECHB fully, CECHC/CECHE partly); hosting from a PS3 has not been tested on SCOPS yet.")
$sockets = New-Object System.Windows.Forms.CheckBox
$sockets.Text = "PCSX2 Sockets mode (use this PC's address)"
$sockets.AutoSize = $true
$sockets.Location = New-Object System.Drawing.Point(110, 236)
$sockets.Visible = $false
$form.Controls.Add($sockets)
$script:ManualPs2 = ""
$script:SocketsBusy = $false
$sockets.Add_CheckedChanged({
    if ($script:SocketsBusy) { return }
    if ($sockets.Checked) {
        $ip = Get-PickedIp
        if (-not $ip) {
            $script:SocketsBusy = $true
            $sockets.Checked = $false
            $script:SocketsBusy = $false
            Set-Status "bad" "No network card" @("This PC has no address to use for Sockets mode. Connect to your home network and try again.")
            return
        }
        $cur = $box.Text.Trim()
        if ($cur -and $cur -ne $ip) { $script:ManualPs2 = $cur }
        $box.Text = $ip
        $box.ReadOnly = $true
        $box.BackColor = [System.Drawing.SystemColors]::Control
        $lbl.Text = "PC address:"
        $idx = $nicBox.SelectedIndex
        if ($idx -ge 0 -and $idx -lt $script:PcAdapters.Count -and $script:PcAdapters[$idx].Virtual) {
            Set-Status "warn" "Sockets on a VPN adapter" @(
                "$ip is a VPN or virtual adapter. Turn the VPN off while hosting, then pick your Wi-Fi or Ethernet card.",
                "Sockets mode sends the game from this PC, so a VPN puts the host behind the VPN.")
        }
    } else {
        $box.ReadOnly = $false
        $box.BackColor = [System.Drawing.SystemColors]::Window
        $lbl.Text = "PS2 address:"
        if ($script:ManualPs2) { $box.Text = $script:ManualPs2 }
    }
})

# V8.17: Find my PS2. Real PS2 (or PCSX2 PCAP Bridged) on the same home network: nudge every address on the picked
# card's /24 with one tiny UDP datagram (port 9), so Windows looks each one up, then read the neighbour (ARP) table and
# keep Sony's registered MAC prefixes. PS2 era: 00:04:1F (2000, PS2 network adapter, also PCSX2), 00:13:15 (2004, slim
# PS2: seen on a real console 2026-11-21) and 00:15:C1 (2005, later slims); the rest are later Sony devices
# (PS3/PS4/PS5...). Same list as scopsDNS.py. No admin rights needed.
$script:Ps2Ouis = @("00041f", "001315", "0015c1")
$script:SonyOuis = @("00041f", "001315", "0015c1", "0019c5", "001d0d", "001fa7", "00248d", "00d9d1", "00e421", "04f778",
    "0c7043", "0cfe45", "280dfc", "2840dd", "2c9e00", "2ccc44", "50b03b", "54e6fd", "5c843c", "5c9666", "68286c", "70662a",
    "709e29", "78c881", "78e04d", "84e657", "904748", "98fa2e", "9c37cb", "a8e3ee", "b40ad8", "b41f4d", "bc3329", "bc60a7",
    "c0151b", "c84aa0", "c863f1", "d4f7d5", "e86e3a", "ec748c", "f46412", "f8461c", "f8d0ac", "fc0fe6", "fcca40", "0022a6")

function ConvertTo-MacKey([string]$m) {
    $parts = @($m.Trim().ToLower() -split "[:\-]")
    if ($parts.Count -ne 6) { return "" }
    $out = ""
    foreach ($x in $parts) { if ($x -notmatch "^[0-9a-f]{1,2}$") { return "" }; $out += $x.PadLeft(2, "0") }
    if ($out -eq "000000000000" -or $out -eq "ffffffffffff") { return "" }
    return $out
}

function Get-NeighbourMacs([bool]$liveOnly = $false) {
    # $liveOnly: only entries Windows has confirmed recently (Reachable/Delay/Probe). A Stale entry can be an old
    # address (the console got a new one after a router restart, or it is switched off), so the quick pass ignores it.
    $t = @{}
    try {
        foreach ($n in @(Get-NetNeighbor -AddressFamily IPv4 -ErrorAction Stop)) {
            if ("$($n.State)" -in @("Unreachable", "Incomplete")) { continue }
            if ($liveOnly -and "$($n.State)" -notin @("Reachable", "Delay", "Probe")) { continue }
            $k = ConvertTo-MacKey "$($n.LinkLayerAddress)"
            if ($k) { $t["$($n.IPAddress)"] = $k }
        }
    } catch {
        if ($liveOnly) { return $t }   # arp.exe shows no freshness: let the caller scan instead
        try {
            foreach ($l in @(& arp.exe -a 2>$null)) {
                if ("$l" -match "^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s") {
                    $k = ConvertTo-MacKey $Matches[2]
                    if ($k) { $t[$Matches[1]] = $k }
                }
            }
        } catch {}
    }
    return $t
}

function Find-Ps2Devices([string]$myIp, [bool]$nudge = $true) {
    $base = $myIp.Substring(0, $myIp.LastIndexOf("."))
    if ($nudge) {
        $u = New-Object System.Net.Sockets.UdpClient
        $b = [System.Text.Encoding]::ASCII.GetBytes("scops")
        for ($i = 1; $i -le 254; $i++) {
            $t = "$base.$i"
            if ($t -eq $myIp) { continue }
            try { [void]$u.Send($b, $b.Length, $t, 9) } catch {}
        }
        $u.Close()
        $until = (Get-Date).AddMilliseconds(2500)
        while ((Get-Date) -lt $until) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 50 }
    }
    $found = @()
    $tab = Get-NeighbourMacs (-not $nudge)
    foreach ($ip in $tab.Keys) {
        if (-not $ip.StartsWith("$base.") -or $ip -eq $myIp) { continue }
        $oui = $tab[$ip].Substring(0, 6)
        if ($script:SonyOuis -contains $oui) {
            $mac = ($tab[$ip] -split "(..)" | Where-Object { $_ }) -join ":"
            $era = if ($script:Ps2Ouis -contains $oui) { 0 } else { 1 }
            $found += New-Object PSObject -Property @{ Ip = $ip; Mac = $mac; Era = $era; Last = [int]$ip.Split(".")[3] }
        }
    }
    return @($found | Sort-Object Era, Last)
}

$findBtn = New-Object System.Windows.Forms.Button
$findBtn.Text = "Find my PS2"
$findBtn.Location = New-Object System.Drawing.Point(410, 232)
$findBtn.Size = New-Object System.Drawing.Size(130, 28)
$form.Controls.Add($findBtn)
$tip.SetToolTip($findBtn, "Looks for the console on your network by its Sony network ID and fills in its address. Start the game's online menu first. PS3: launch models only.")
$script:HostMode = "ps2"
function Sync-HostMode {
    $i = $modeBox.SelectedIndex
    $script:HostMode = @("ps2", "pcap", "sockets", "ps3")[$i]
    $wantSockets = ($script:HostMode -eq "sockets")
    if ($sockets.Checked -ne $wantSockets) { $sockets.Checked = $wantSockets }
    $findBtn.Enabled = -not $wantSockets
    $findBtn.Text = if ($script:HostMode -eq "ps3") { "Find my PS3" } else { "Find my PS2" }
    if ($script:HostMode -eq "ps3") { $lbl.Text = "PS3 address:" }
    elseif ($wantSockets) { $lbl.Text = "PC address:" }
    else { $lbl.Text = "PS2 address:" }
}
$modeBox.Add_SelectedIndexChanged({ Sync-HostMode })
$sockets.Add_CheckedChanged({ $findBtn.Enabled = -not $sockets.Checked })
$findBtn.Add_Click({
    $myIp = Get-PickedIp
    if (-not $myIp) {
        Set-Status "bad" "No network card" @("Pick your Wi-Fi or Ethernet card at the top first.")
        return
    }
    $isPs3 = ($script:HostMode -eq "ps3")
    $name = if ($isPs3) { "PS3" } else { "PS2" }
    $findBtn.Enabled = $false
    Set-Status "busy" "Looking for your $name..." @(
        "Checking this network for a Sony adapter. The $($name.ToLower()) must be at the game's online menu.")
    $form.Refresh()
    [System.Windows.Forms.Application]::DoEvents()
    $res = @()
    try { $res = @(Find-Ps2Devices $myIp $false) } catch { Write-ScopsLog "Find (table) error: $($_.Exception.Message)" }
    $primaryEra = if ($isPs3) { 1 } else { 0 }
    $havePrimary = @($res | Where-Object { $_.Era -eq $primaryEra }).Count -gt 0
    if (-not $havePrimary) {
        Set-Status "busy" "Looking for your $name..." @(
            "Nothing in the neighbour table yet. Checking every address on $($myIp.Substring(0, $myIp.LastIndexOf('.'))).x (a few seconds).")
        $form.Refresh()
        [System.Windows.Forms.Application]::DoEvents()
        try { $res = @(Find-Ps2Devices $myIp $true) } catch { $res = @(); Write-ScopsLog "Find my $name error: $($_.Exception.Message)" }
    }
    $findBtn.Enabled = -not $sockets.Checked
    foreach ($r in $res) { Write-ScopsLog "Find my ${name}: $($r.Ip) MAC $($r.Mac) era=$($r.Era)" }
    $primaryEra = if ($isPs3) { 1 } else { 0 }
    $primary = @($res | Where-Object { $_.Era -eq $primaryEra })
    $others = @($res | Where-Object { $_.Era -ne $primaryEra })
    if ($primary.Count -ge 1) {
        $box.Text = $primary[0].Ip
        if ($isPs3) {
            # Not a PS2 ID only means "a later Sony device": PS3, but also PS4, PS5 or PSP. Never claim it is the PS3.
            $lines = @("$($primary[0].Ip) is a Sony device that is not a PS2 (MAC $($primary[0].Mac)). Filled in above.")
            if ($primary.Count -gt 1) {
                $lines += "Sony devices found: " + (($primary | ForEach-Object { "$($_.Ip) ($($_.Mac))" }) -join ", ") + "."
                $lines += "One of these may be a PS4 or PS5. Check the PS3's address (PS3: Settings > Network Settings > Settings and Connection Status List) and type it if it differs."
            } else {
                $lines += "If you also have a PS4 or PS5 switched on, check this is the PS3 (PS3: Settings > Network Settings > Settings and Connection Status List)."
            }
            if ($others.Count) { $lines += "PS2-era devices also found: " + (($others | ForEach-Object { $_.Ip }) -join ", ") }
            $lines += "Only early PS3s play PS2 games (CECHA/CECHB fully, CECHC/CECHE partly). Hosting from a PS3 has not been tested on SCOPS yet."
            Set-Status "warn" "Check this is your PS3: $($primary[0].Ip)" $lines
        } else {
            $lines = @("$($primary[0].Ip) has a PS2-era Sony network adapter (MAC $($primary[0].Mac)). Filled in above. Press Check UPnP.")
            if ($primary.Count -gt 1) { $lines += "Also found: " + (($primary[1..($primary.Count - 1)] | ForEach-Object { $_.Ip }) -join ", ") + ". If the first is not yours, type one of these." }
            if ($others.Count) { $lines += "Other Sony devices: " + (($others | ForEach-Object { $_.Ip }) -join ", ") }
            Set-Status "ok" "$name found: $($primary[0].Ip)" $lines
        }
    } elseif ($others.Count -eq 1) {
        $box.Text = $others[0].Ip
        Set-Status "warn" "Sony device found: $($others[0].Ip)" @(
            "$($others[0].Ip) is a Sony device (MAC $($others[0].Mac)), but not the usual ID for a $name.",
            "Filled in above. If it is not your console, check the address in its network settings.")
    } elseif ($others.Count -gt 1) {
        Set-Status "warn" "Several Sony devices" @(
            "Found: " + (($others | ForEach-Object { "$($_.Ip) ($($_.Mac))" }) -join ", "),
            "None has the usual ID for a $name. Check the address in the console's network settings and type it above.")
    } else {
        Set-Status "warn" "$name not found" @(
            "No Sony network device answered on this network.",
            "  - Start the game's online menu on the $name (past the network connection screen), then press Find again.",
            "  - The console and this PC must be on the same home network (not a guest Wi-Fi).",
            "  - Or type the address from the console's network settings.")
    }
})

$script:Gateway = ""
$script:Svc = $null
$script:Ps2 = ""
$script:Mapped = @()
# Router brand -> portforward.com guide index (each link checked on 2026-11-17). Anything else: the general list.
$PfGeneral = "https://portforward.com/router.htm"
$PfBrands = [ordered]@{
    "asus" = "https://portforward.com/asus/"; "tp-?link" = "https://portforward.com/tp-link/";
    "netgear" = "https://portforward.com/netgear/"; "linksys" = "https://portforward.com/linksys/";
    "d-?link" = "https://portforward.com/d-link/"; "arris|surfboard" = "https://portforward.com/arris/";
    "technicolor" = "https://portforward.com/technicolor/"; "sagemcom" = "https://portforward.com/sagemcom/"
}
$BrandNames = @{ "asus" = "ASUS"; "tp-?link" = "TP-Link"; "netgear" = "Netgear"; "linksys" = "Linksys";
                 "d-?link" = "D-Link"; "arris|surfboard" = "Arris"; "technicolor" = "Technicolor"; "sagemcom" = "Sagemcom" }
$script:RouterInfo = ""     # maker/model text from the router's UPnP description, if the check got that far
$script:Brand = $null       # @{ Name; Url; Model } once known

function Get-GatewayPageText([string]$gw) {
    # The router's own login page usually names the maker (page title, login realm or server name). Many routers
    # (ASUS among them) answer "/" with a small page that sends the browser on by script; follow that once.
    $t = Get-PageText "http://$gw/"
    $next = $null
    if ($t -match "(?i)location(?:\.href)?\s*=\s*[`"']([^`"']{1,200})[`"']") { $next = $Matches[1] }
    elseif ($t -match "(?i)http-equiv=[`"']?refresh[^>]*url=([^`"'>\s]{1,200})") { $next = $Matches[1] }
    if ($next) {
        try { $t += " " + (Get-PageText ([Uri]::new([Uri]"http://$gw/", $next)).AbsoluteUri) } catch {}
    }
    return $t
}

function Get-PageText([string]$url) {
    $out = ""
    try {
        $req = [System.Net.HttpWebRequest]::Create($url)
        $req.Timeout = 3000; $req.ReadWriteTimeout = 3000; $req.Proxy = $null; $req.KeepAlive = $false
        $resp = $null
        try { $resp = $req.GetResponse() } catch {
            $ex = $_.Exception
            while ($ex -and -not ($ex -is [System.Net.WebException])) { $ex = $ex.InnerException }
            if ($ex -and $ex.Response) { $resp = $ex.Response }
        }
        if ($resp) {
            $out = "$($resp.Headers['WWW-Authenticate']) $($resp.Headers['Server'])"
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            $buf = New-Object char[] 8192
            $n = $sr.Read($buf, 0, $buf.Length)
            $txt = -join $buf[0..([Math]::Max(0, $n - 1))]
            if ($txt -match "<title[^>]*>([^<]{1,120})</title>") { $out += " " + $Matches[1] }
            $out += " " + $txt
            $resp.Close()
        }
    } catch {}
    return $out
}

function Find-RouterBrand {
    if ($script:Brand) { return $script:Brand }
    $text = $script:RouterInfo
    $model = ""
    if ($text) { $model = $text }
    if (-not ($text -match "asus|tp-?link|netgear|linksys|d-?link|arris|surfboard|technicolor|sagemcom") -and $script:Gateway) {
        $text = "$text " + (Get-GatewayPageText $script:Gateway)
    }
    if (-not ($text -match "(?i)asus|tp-?link|netgear|linksys|d-?link|arris|surfboard|technicolor|sagemcom")) {
        # Read-only: ask the router for its UPnP description (maker and model). Adds no forward.
        try {
            $cands = @(Get-LanCandidates)
            if ($cands.Count -gt 0) {
                $found = Find-Igd $cands 2000
                foreach ($loc in $found.Keys) {
                    $sv = Get-WanService $loc
                    if ($sv -and ($sv.Make -or $sv.Model)) {
                        $script:RouterInfo = "$($sv.Make) $($sv.Model)".Trim()
                        $model = $script:RouterInfo
                        $text = "$text $($script:RouterInfo)"
                        break
                    }
                }
            }
        } catch {}
    }
    foreach ($k in $PfBrands.Keys) {
        if ($text -match "(?i)\b($k)") {
            $script:Brand = @{ Name = $BrandNames[$k]; Url = $PfBrands[$k]; Model = $model }
            return $script:Brand
        }
    }
    $script:Brand = @{ Name = ""; Url = $PfGeneral; Model = $model }
    return $script:Brand
}

function Show-ForwardGuide {
    $ps2 = $box.Text.Trim()
    if (-not $ps2) { $ps2 = "your PS2 address" }
    $detail.Text = (@(
        "PORT FORWARD BY HAND (best if you can)",
        "",
        "1. Log in on the router page that just opened ($($script:Gateway)). The username and password are often on a",
        "   sticker on the router, unless someone changed them. Ask whoever set up the router if you do not know them.",
        "2. Find Port Forwarding. It can also be called Virtual Server, NAT, Port Mapping or Applications & Gaming.",
        "3. Add these two rules, both pointing at $ps2 :",
        "     UDP  3658          ->  $ps2  port 3658",
        "     UDP  10070-10080   ->  $ps2  same ports",
        "4. Save / Apply.",
        "5. Give the PS2 (or the PC, in PCSX2 Sockets mode) a fixed address in the router (often called DHCP",
        "   reservation or manual assignment), so the forward keeps pointing at it.",
        "",
        "With a hand-made forward you do not need Check UPnP; do not use both for the same ports.",
        "",
        "ASUS: WAN > Virtual Server / Port Forwarding. Leave hardware (NAT) acceleration on.",
        "DMZ is a last resort only: use it for a PS2 console, never a PC (it opens every port), and turn it off after playing.",
        "Not sure? Skip it - you can still host; the SCOPS server relay gets players to you."
    ) -join "`r`n")
    $b = Find-RouterBrand
    if ($b.Name) {
        $detail.AppendText("`r`n`r`nYour router looks like: $($b.Name)" + $(if ($b.Model) { " ($($b.Model))" } else { "" }) +
                           ". Step-by-step pictures for $($b.Name) routers: press 'Guide for my router' ($($b.Url)).")
    } else {
        $detail.AppendText("`r`n`r`nCould not tell which router this is. Press 'Guide for my router' and pick yours from the list " +
                           "at portforward.com (the maker's name is usually on the router's label).")
    }
    $guide.Visible = $true
}
$router.Add_Click({
    if (-not $script:Gateway) { return }
    $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    try { Show-ForwardGuide } finally { $form.Cursor = [System.Windows.Forms.Cursors]::Default }
    try { Start-Process "http://$($script:Gateway)/" } catch {
        $detail.AppendText("`r`n`r`nCould not open the browser. Type http://$($script:Gateway)/ into your browser.")
    }
})
try {
    $c0 = @(Get-LanCandidates)
    if ($c0.Count -gt 0 -and $c0[0].Gw) {
        $script:Gateway = $c0[0].Gw
        $router.Visible = $true
        $routerNote.Visible = $true
    }
} catch {}

function Set-Status([string]$state, [string]$text, $lines) {
    $script:State = $state
    $head.Text = $text
    $head.ForeColor = switch ($state) {
        "ok"   { [System.Drawing.Color]::FromArgb(30, 120, 50) }
        "bad"  { [System.Drawing.Color]::FromArgb(180, 25, 35) }
        "warn" { [System.Drawing.Color]::FromArgb(160, 100, 0) }
        default { [System.Drawing.SystemColors]::ControlText }
    }
    $detail.Text = (@($lines) -join "`r`n")
    $icon.Invalidate()
}

# last address used, for convenience. Line 2 is "sockets" when PCSX2 Sockets mode was on.
$cfgDir = Join-Path $env:APPDATA "SCOPS"
$cfgFile = Join-Path $cfgDir "upnp_ps2_address.txt"
try {
    if (Test-Path $cfgFile) {
        $lines = @(Get-Content $cfgFile -ErrorAction Stop)
        $script:ManualPs2 = "$($lines[0])".Trim()
        $box.Text = $script:ManualPs2
        $savedMode = if ($lines.Count -gt 1) { "$($lines[1])".Trim() } else { "" }
        if ($savedMode -eq "sockets") { $modeBox.SelectedIndex = 2 }
        elseif ($savedMode -eq "pcap") { $modeBox.SelectedIndex = 1 }
        elseif ($savedMode -eq "ps3") { $modeBox.SelectedIndex = 3 }
        else { $modeBox.SelectedIndex = 0 }
        Sync-HostMode
    }
} catch {}

$script:Refresh = New-Object System.Windows.Forms.Timer
$script:Refresh.Interval = 20 * 60 * 1000
function Find-WanServiceAgain {
    # The router's UPnP service can come back on a new address or port (ASUS restarts it with a new port whenever
    # router settings are applied, e.g. turning DMZ off). Look for it again the same way the check does.
    $cands = @(Get-LanCandidates)
    if ($cands.Count -eq 0) { return $null }
    $found = Find-Igd $cands
    foreach ($loc in $found.Keys) {
        $svc = Get-WanService $loc
        if ($svc) { return $svc }
    }
    return $null
}

$script:Refresh.Add_Tick({
    if (-not $script:Svc) { return }
    $now = Get-Date -Format HH:mm
    try {
        $total = @($GamePorts).Count
        $r = Invoke-MapAll $script:Svc $script:Ps2
        $again = ""
        if (@($r.Ok).Count -lt $total) {
            # 2026-11-17: on the ASUS every renewal failed ("0 ports") and the forwards ran out after an hour.
            $first = @($r.Bad)[0]
            $svc2 = Find-WanServiceAgain
            if ($svc2) {
                $script:Svc = $svc2
                $r = Invoke-MapAll $script:Svc $script:Ps2
                $again = " after finding the router again"
            } else {
                $again = " (router not found again)"
            }
            if (@($r.Ok).Count -lt $total -and $first) { $again += ". First error: $first" }
        }
        $n = @($r.Ok).Count
        if ($n -gt 0) { $script:Mapped = @($r.Ok) }
        if ($n -eq 0) { $script:OpenFlag = $false }
        $flag = ""
        if ($script:OpenFlag -and $n -eq $total) {
            if (Send-OpenFlag) { $flag = ", SCOPS server notified" } else { $flag = ", could not reach the SCOPS server" }
        }
        if ($n -eq $total) {
            $detail.AppendText("`r`n[$now] forwards renewed for another hour ($n of $total ports$again$flag)")
        } else {
            $detail.AppendText("`r`n[$now] RENEWAL FAILED: $n of $total ports renewed$again$flag. The forwards " +
                               "will run out within the hour. Press Check UPnP again, or send a screenshot of this window.")
            @($r.Bad) | Select-Object -First 3 | ForEach-Object { $detail.AppendText("`r`n    $_") }
        }
    } catch {
        $detail.AppendText("`r`n[$now] RENEWAL FAILED: $($_.Exception.Message). Press Check UPnP again.")
    }
})

function Remove-Ours {
    if (-not $script:Svc -or @($script:Mapped).Count -eq 0) { return }
    foreach ($p in $script:Mapped) {
        try {
            $m = Get-Mapping $script:Svc $p
            if ($m -and ($m.Desc -like "SCCT*")) { [void](Remove-Mapping $script:Svc $p 1500) }
        } catch {}
    }
    $script:Mapped = @()
}

$btn.Add_Click({
    if ($sockets.Checked) {
        $picked = Get-PickedIp
        if ($picked) { $box.Text = $picked }
    }
    $ip = $box.Text.Trim()
    $okIp = $false
    $addr = $null
    if ($ip -match '^\d{1,3}(\.\d{1,3}){3}$' -and [System.Net.IPAddress]::TryParse($ip, [ref]$addr)) { $okIp = $true }
    if (-not $okIp) {
        Set-Status "bad" "That is not an address" @("Type the address as four numbers with dots, for example 192.168.1.240. For PCSX2 Sockets, pick that under Hosting with.")
        return
    }
    $script:SocketsFwNote = ""
    $script:PublicNote = ""
    if ($sockets.Checked) {
        # V8.16: the Sockets rule is for Private/Domain networks only; Windows 11 often marks a home network Public.
        try {
            $pickedIp = $box.Text.Trim()
            $ifx = (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $pickedIp -ErrorAction Stop | Select-Object -First 1).InterfaceIndex
            $cat = (Get-NetConnectionProfile -InterfaceIndex $ifx -ErrorAction Stop | Select-Object -First 1).NetworkCategory
            Write-ScopsLog "Sockets: network category of $pickedIp is $cat"
            if ("$cat" -eq "Public") {
                $script:PublicNote = "Your network is set to Public in Windows, so the Sockets firewall rule does not apply and players may not reach this PC directly (they still get in through the relay). Fix: Windows Settings > Network & internet > Ethernet or Wi-Fi > your network > Network profile type: Private."
            }
        } catch {
            Write-ScopsLog "Sockets: network category not read ($($_.Exception.Message))"
        }
        if (Test-SocketsFirewall) {
            $script:SocketsFwNote = "Windows firewall: the Sockets rule is already in place (UDP 3658 and 10070-10080)."
        } else {
            Set-Status "busy" "Allow the game in Windows..." @(
                "Windows will ask once to allow UDP 3658 and 10070-10080 on this PC. Say yes.",
                "This is only for PCSX2 Sockets mode, because the game arrives on this PC.")
            $form.Refresh()
            [System.Windows.Forms.Application]::DoEvents()
            if (Add-SocketsFirewall) {
                $script:SocketsFwNote = "Windows firewall: allowed UDP 3658 and 10070-10080 on this PC."
            } else {
                $script:SocketsFwNote = "Windows firewall: not changed (the prompt was declined). If players cannot join, allow PCSX2 when Windows asks."
            }
        }
    }
    try {
        if (-not (Test-Path $cfgDir)) { [void](New-Item -ItemType Directory $cfgDir) }
        $saveAddr = $ip
        if ($sockets.Checked -and $script:ManualPs2) { $saveAddr = $script:ManualPs2 }
        $modeLine = if ($script:HostMode) { $script:HostMode } elseif ($sockets.Checked) { "sockets" } else { "ps2" }
        Set-Content $cfgFile @($saveAddr, $modeLine)
    } catch {}
    if ($script:Svc -and $script:Ps2 -and $script:Ps2 -ne $ip) { Remove-Ours }   # address changed: drop the old forwards
    $script:Refresh.Stop()
    $btn.Enabled = $false
    Set-Status "busy" "Checking your router..." @("Looking for a UPnP router on this network. This takes a few seconds.")
    $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
    $form.Refresh()
    [System.Windows.Forms.Application]::DoEvents()
    $r = $null
    try {
        $r = Invoke-Check $ip
    } catch {
        $e = $_
        Set-Status "bad" "The check failed" @("Something went wrong while checking. Please send a screenshot of this window.", "",
            "$($e.Exception.GetType().FullName): $($e.Exception.Message)", "",
            "$($e.InvocationInfo.PositionMessage)", "", "$($e.ScriptStackTrace)")
        if ($script:SocketsFwNote) { $detail.AppendText("`r`n`r`n$($script:SocketsFwNote)") }
    if ($script:PublicNote) { $detail.AppendText("`r`n`r`n$($script:PublicNote)") }
        if ($script:PublicNote) { $detail.AppendText("`r`n`r`n$($script:PublicNote)") }
    }
    $form.Cursor = [System.Windows.Forms.Cursors]::Default
    $btn.Enabled = $true
    if (-not $r) { return }
    if ($r -is [System.Management.Automation.PSObject]) { $r = $r.BaseObject }
    $script:OpenFlag = $false
    Set-Status $r.State $r.Head $r.Lines
    if ($script:SocketsFwNote) { $detail.AppendText("`r`n`r`n$($script:SocketsFwNote)") }
    if ($script:PublicNote) { $detail.AppendText("`r`n`r`n$($script:PublicNote)") }
    if ($r.Gateway -and $r.Gateway -ne $script:Gateway) { $script:Gateway = $r.Gateway; $script:Brand = $null }
    if ($r.Svc -and ($r.Svc.Make -or $r.Svc.Model)) {
        $info = "$($r.Svc.Make) $($r.Svc.Model)".Trim()
        if ($info -ne $script:RouterInfo) { $script:RouterInfo = $info; $script:Brand = $null }
    }
    $router.Visible = [bool]$script:Gateway
    $routerNote.Visible = $router.Visible
    if ($r.Svc -and @($r.Mapped).Count -gt 0) {
        $script:Svc = $r.Svc
        $script:Ps2 = $ip
        $script:Mapped = @($r.Mapped)
        if ($r.State -eq "ok") {
            $script:OpenFlag = $true
            $flagOk = Send-OpenFlag
            if (-not $flagOk) {
                $detail.AppendText("`r`nCould not tell the SCOPS server you are open. The forwards are still in place.")
            }
        }
        if ($r.State -eq "ok") { Start-RouterTest $r.Svc }
        $script:Refresh.Start()
    }
})

function Stop-ScopsDns {
    $ids = @()
    if ($script:DnsProc) { $ids += $script:DnsProc.Id }
    try {
        $ours = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "scopsDNS\.(py|exe|ps1)" })
        foreach ($p in $ours) { $ids += $p.ProcessId }
    } catch {
        Write-ScopsLog "leftover lookup failed: $($_.Exception.Message)"
    }
    $ids = @($ids | Select-Object -Unique)
    foreach ($id in $ids) {
        Write-ScopsLog "stopping DNS pid $id"
        try { & taskkill /F /T /PID $id 2>&1 | Out-Null } catch { Write-ScopsLog "kill $id failed: $_" }
    }
    Start-Sleep -Milliseconds 400
    $still = @()
    try {
        $still = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "scopsDNS\.(py|exe|ps1)" })
    } catch {}
    foreach ($p in $still) {
        Write-ScopsLog "still running after kill, forcing pid $($p.ProcessId)"
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($still.Count -gt 0) { Start-Sleep -Milliseconds 300 }
    $left = @()
    try {
        $left = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "scopsDNS\.(py|exe|ps1)" })
    } catch {}
    $held = @()
    try {
        $held = @(netstat -ano | Select-String ":\s*53\s" | ForEach-Object {
            $parts = ($_ -split "\s+") | Where-Object { $_ }
            $parts[-1]
        } | Select-Object -Unique)
    } catch {}
    if ($left.Count -eq 0) {
        Write-ScopsLog "DNS terminated. Port 53 holders: $(if ($held) { $held -join ',' } else { 'none' })"
    } else {
        Write-ScopsLog "DNS still lingering: $($left.ProcessId -join ', '). Port 53 holders: $(if ($held) { $held -join ',' } else { 'none' })"
    }
}

$form.Add_FormClosing({
    if (-not $script:CloseOk -and @($script:Mapped).Count -gt 0 -and
        $_.CloseReason -eq [System.Windows.Forms.CloseReason]::UserClosing) {
        $a = [System.Windows.Forms.MessageBox]::Show($form,
            "Closing removes the port forwards. Hosting will fall back to the relay.`r`n`r`nClose anyway?",
            "SCOPS", [System.Windows.Forms.MessageBoxButtons]::YesNo, [System.Windows.Forms.MessageBoxIcon]::Warning)
        if ($a -ne [System.Windows.Forms.DialogResult]::Yes) { $_.Cancel = $true; return }
        $script:CloseOk = $true
    }
    Write-ScopsLog ("window closing, reason=" + $form.CloseReason)
    if ($script:DnsTimer) { try { $script:DnsTimer.Stop() } catch {} }
    $script:Refresh.Stop()
    Stop-RouterTest
    if (@($script:Mapped).Count -gt 0) {
        $form.Cursor = [System.Windows.Forms.Cursors]::WaitCursor
        Remove-Ours
    }
    Stop-ScopsDns
})

$script:DnsProc = $null
$script:form = $form
$script:dnsHead = $dnsHead
$script:dnsAddr = $dnsAddr
$script:dnsDetail = $dnsDetail
$script:dnsIcon = $dnsIcon
$script:LogFile = Join-Path (Split-Path -Parent $PSCommandPath) "scops-window.log"

function Write-ScopsLog([string]$msg) {
    try {
        $dir = Split-Path $script:LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
        $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $msg
        Add-Content -Path $script:LogFile -Value $line -Encoding UTF8
    } catch {}
}

Write-ScopsLog "---- window opened ----"
[System.Windows.Forms.Application]::add_ThreadException({
    Write-ScopsLog ("UI exception: " + $_.Exception.Message)
})

function Set-Dns([string]$state, [string]$head, [string]$detail) {
    if ($script:form.IsDisposed) { return }
    $script:form.BeginInvoke([Action]{
        $script:DnsState = $state
        $script:dnsHead.Text = $head
        $script:dnsHead.ForeColor = switch ($state) {
            "ok"   { [System.Drawing.Color]::FromArgb(30, 120, 50) }
            "bad"  { [System.Drawing.Color]::FromArgb(180, 25, 35) }
            "warn" { [System.Drawing.Color]::FromArgb(160, 100, 0) }
            default { [System.Drawing.SystemColors]::ControlText }
        }
        if ($detail) { $script:dnsDetail.Text = $detail }
        $script:dnsIcon.Invalidate()
    }) | Out-Null
}

# V8.15: find a Python 3 that really runs. Windows ships a "python.exe" placeholder in ...\WindowsApps that only says
# "Python was not found" and exits 9009 (2026-11-20 BLACKNIGHT: the DNS check ran it twice and showed "no answer").
# Every candidate is test-run; a WindowsApps path is used only if it is a real Store Python that runs.
function Find-RealPython {
    $cands = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $found = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $cands += ($found | Select-Object -Last 1).ToString().Trim() }
        } catch {}
    }
    foreach ($name in @("python", "python3")) {
        foreach ($c in @(Get-Command $name -All -ErrorAction SilentlyContinue)) { if ($c.Source) { $cands += $c.Source } }
    }
    foreach ($c in ($cands | Select-Object -Unique)) {
        if (-not $c -or -not (Test-Path $c)) { continue }
        try {
            $out = & $c -c "import sys; print(sys.version_info[0])" 2>$null
            if ($LASTEXITCODE -eq 0 -and ($out | Select-Object -Last 1) -eq "3") { return $c }
            Write-ScopsLog "Python candidate skipped (does not run, exit $LASTEXITCODE): $c"
        } catch {
            Write-ScopsLog "Python candidate skipped ($($_.Exception.Message)): $c"
        }
    }
    return $null
}

# V8.16: a scopsDNS.exe built from an old scopsDNS.py has no --check-only and fails every check ("unrecognized
# arguments", exit 2; 2026-11-21). Ask it for --help once; an exe that does not list --check-only is skipped.
$script:DnsExeOk = $null
function Test-DnsExe([string]$path) {
    if ($null -ne $script:DnsExeOk) { return $script:DnsExeOk }
    $ok = $false
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $path
        $psi.Arguments = "--help"
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $txt = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
        if (-not $p.WaitForExit(15000)) { try { $p.Kill() } catch {} }
        $ok = ($txt -match "--check-only")
        if (-not $ok) { Write-ScopsLog "scopsDNS.exe is an old build (no --check-only); skipped. Rebuild it from scopsDNS.py with build_exe.bat, or delete it." }
    } catch {
        Write-ScopsLog "scopsDNS.exe could not run ($($_.Exception.Message)); skipped"
    }
    $script:DnsExeOk = $ok
    return $ok
}

# V8.15: which DNS tool to run. scopsDNS.exe, else a real Python 3 with scopsDNS.py, else the built-in scopsDNS.ps1
# (Windows PowerShell, on every Windows 10/11: nothing to install). Returns @{ Exe; Arg; Kind } or $null.
# $mode "check" = the start-up check; "serve" = the local DNS for a network that hijacks DNS.
function Get-DnsTool([string]$mode) {
    $dir = Split-Path -Parent $PSCommandPath
    $exePath = Join-Path $dir "scopsDNS.exe"
    $pyPath = Join-Path $dir "scopsDNS.py"
    $psPath = Join-Path $dir "scopsDNS.ps1"
    $pyArgs = if ($mode -eq "check") { "--check-only --no-firewall --no-upnp" } else { "--no-upnp" }
    $psArgs = if ($mode -eq "check") { "-CheckOnly -NoFirewall -NoUpnp" } else { "-NoUpnp" }
    if ((Test-Path $exePath) -and (Test-DnsExe $exePath)) { return @{ Exe = $exePath; Arg = $pyArgs; Kind = "scopsDNS.exe"; IpFlag = "--ip" } }
    if (Test-Path $pyPath) {
        $py = Find-RealPython
        if ($py) { return @{ Exe = $py; Arg = "`"$pyPath`" $pyArgs"; Kind = "scopsDNS.py (Python)"; IpFlag = "--ip" } }
    }
    if (Test-Path $psPath) {
        $psExe = Join-Path $PSHOME "powershell.exe"
        if (-not (Test-Path $psExe)) { $psExe = "powershell.exe" }
        return @{ Exe = $psExe; Arg = "-NoProfile -ExecutionPolicy Bypass -File `"$psPath`" $psArgs"; Kind = "scopsDNS.ps1 (built-in)"; IpFlag = "-Ip" }
    }
    return $null
}

function Start-ScopsDns {
    $dir = Split-Path -Parent $PSCommandPath
    $tool = Get-DnsTool "check"
    if (-not $tool) {
        Write-ScopsLog "DNS check: no scopsDNS.exe, no working Python 3 and no scopsDNS.ps1 in this folder"
        $script:DnsState = "warn"
        Set-Dns "warn" "DNS check could not start" "scopsDNS.ps1 is missing from this folder. Unzip the whole SCOPS folder again, then press Check again. You can still put 38.49.212.120 in the PS2."
        try { $dnsRetry.Visible = $true } catch {}
        return
    }
    $exe = $tool.Exe
    $arg = $tool.Arg
    $script:DnsToolKind = $tool.Kind
    $script:DnsMode = "check"
    $script:DnsEndedAt = $null
    $script:DnsTries++
    $script:DnsCheckStart = Get-Date
    $script:DnsState = "busy"
    Write-ScopsLog "DNS check (try $($script:DnsTries)): $exe $arg"
    try {
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'cmd.exe' OR Name = 'powershell.exe' OR Name = 'scopsDNS.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "scopsDNS\.(py|exe|ps1)" } |
            ForEach-Object {
                Write-ScopsLog "closing leftover DNS pid $($_.ProcessId)"
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
        Start-Sleep -Milliseconds 400
    } catch {
        Write-ScopsLog "leftover check failed: $($_.Exception.Message)"
    }
    $dnsLog = Join-Path $dir "scops-dns.log"
    if (Test-Path $dnsLog) { Remove-Item $dnsLog -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType File -Path $dnsLog -Force | Out-Null
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "cmd.exe"
    $psi.Arguments = "/c `"`"$exe`" $arg > `"$dnsLog`" 2>&1`""
    $psi.WorkingDirectory = $dir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try { [void]$proc.Start() } catch {
        Write-ScopsLog "DNS start failed: $($_.Exception.Message)"
        Set-Dns "bad" "DNS could not start" $_.Exception.Message
        return
    }
    Write-ScopsLog "DNS pid $($proc.Id), output $dnsLog"
    $script:DnsProc = $proc
    $script:DnsLog = $dnsLog
    $script:DnsRead = $null
    try {
        $fs = [System.IO.File]::Open($dnsLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $script:DnsRead = New-Object System.IO.StreamReader($fs)
    } catch {
        Write-ScopsLog "could not open DNS log: $($_.Exception.Message)"
    }

    $script:DnsTimer = New-Object System.Windows.Forms.Timer
    $script:DnsTimer.Interval = 300
    $script:DnsBeat = 0
    $script:DnsLastAlive = ""
    $script:DnsTimer.Add_Tick({
        try {
            $script:DnsBeat++
            if (($script:DnsBeat % 10) -eq 0) {
                # V8.14: log only when the DNS state changes, plus once a minute while it runs (was every 3 s, which
                # filled the Log window with "alive, DNS exited 0" after a finished check).
                $alive = if ($script:DnsProc -and -not $script:DnsProc.HasExited) { "running" } else { "exited $($script:DnsProc.ExitCode)" }
                if ($alive -ne $script:DnsLastAlive -or ($alive -eq "running" -and ($script:DnsBeat % 200) -eq 0)) {
                    Write-ScopsLog "DNS $alive"
                    $script:DnsLastAlive = $alive
                }
            }
            if ($script:DnsRead) {
                while ($script:DnsRead.Peek() -ge 0) {
                    $line = $script:DnsRead.ReadLine()
                    if ($null -eq $line) { break }
                    Write-ScopsLog "dns: $line"
                    if ($line -match "PS2 address seen: (\d{1,3}(?:\.\d{1,3}){3})") {
                        $seen = $Matches[1]
                        if (-not $sockets.Checked -and -not $box.Text.Trim()) {
                            $box.Text = $seen
                            Write-ScopsLog "PS2 address filled from its DNS request: $seen"
                        }
                    }
                    if ($line -match "Primary DNS:\s+(\S+)") {
                        $dnsAddr.Text = "Put this in the PS2, both DNS entries:  $($Matches[1])"
                    }
                    elseif ($line -match "no hijack detected") {
                        $script:DnsState = "ok"
                        $dnsHead.Text = "No changes needed"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(30, 120, 50)
                        $dnsAddr.Text = "Put this in the PS2, both DNS entries:  38.49.212.120"
                        $dnsDetail.Text = "Your ISP allows you to use the SCOPS DNS directly."
                        $dnsBtn.Visible = $false
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "PC DNS filter") {
                        $script:DnsState = "warn"
                        $dnsHead.Text = "Something else answers DNS here"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(160, 100, 0)
                        $dnsAddr.Text = "Put this in the PS2, both DNS entries:  38.49.212.120"
                        $dnsDetail.Text = "The check was answered by something on this PC or your network (a VPN, antivirus, DNS filter, or your provider). " +
                            "The PS2 may still work: put 38.49.212.120 in the console first. If the game does not get online (DNAS error), use local DNS."
                        $dnsBtn.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "hijack detected") {
                        $script:DnsState = "bad"
                        $why = ""
                        if ($line -match "detected:\s*(.+?), not ") { $why = $Matches[1] + ". " }
                        $dnsHead.Text = "DNS is being redirected"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(180, 25, 35)
                        $dnsDetail.Text = "SCOPS did not answer the check: " + $why +
                            "A PS2 set to 38.49.212.120 will not work from this network. Use local DNS."
                        $dnsBtn.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "server itself is reachable") {
                        $script:DnsState = "warn"
                        $dnsHead.Text = "Check again in a minute"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(160, 100, 0)
                        $dnsAddr.Text = "Put this in the PS2, both DNS entries:  38.49.212.120"
                        $dnsDetail.Text = "The SCOPS server is reachable (it refused a test name, as it should), but a game name did not come back. Try the check again. You can still put 38.49.212.120 in the PS2."
                        $dnsBtn.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "Could not get a SCOPS answer") {
                        $script:DnsState = "bad"
                        $dnsHead.Text = "SCOPS DNS did not answer"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(180, 25, 35)
                        $dnsDetail.Text = "This PC could not get 38.49.212.120 back from the SCOPS DNS. Use local DNS."
                        $dnsBtn.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "Could not reach any public resolver") {
                        $script:DnsState = "warn"
                        $dnsHead.Text = "Could not check for hijack"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(160, 100, 0)
                        $dnsDetail.Text = "Could not check. You can still use local DNS."
                        $dnsBtn.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "Permission error|Cannot listen|\[ERROR\]") {
                        $script:DnsState = "bad"
                        $dnsHead.Text = "DNS could not start"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(180, 25, 35)
                        $dnsDetail.Text = $line
                        $dnsIcon.Invalidate()
                    }
                    elseif ($line -match "scopsDNS is ready") {
                        if ($script:DnsState -eq "busy") {
                            $script:DnsState = "ok"
                            $dnsHead.Text = "DNS is running"
                            $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(30, 120, 50)
                            $dnsIcon.Invalidate()
                        }
                    }
                    elseif ($line -match "looked up (\S+) ->") {
                        $dnsDetail.Text = "Console looked up $($Matches[1]). DNS is working."
                    }
                }
            }
            if ($script:DnsMode -eq "check" -and $script:DnsState -eq "busy" -and $script:DnsProc) {
                # V8.13: the check ended (or hung) without a verdict line. Try once more by itself, then offer "Check again".
                $ended = $script:DnsProc.HasExited
                $secs = ((Get-Date) - $script:DnsCheckStart).TotalSeconds
                # V8.15: the check can finish (exit 0) before this window has read its last lines from scops-dns.log.
                # 2026-11-20: try 1 answered in 0.6 s, was called "no answer" on the same tick, and try 2 deleted its
                # log. Keep reading for 2 s after the check ends; only then call it "no answer".
                if ($ended -and -not $script:DnsEndedAt) { $script:DnsEndedAt = Get-Date }
                $endedFor = if ($script:DnsEndedAt) { ((Get-Date) - $script:DnsEndedAt).TotalSeconds } else { 0 }
                if (($ended -and $endedFor -ge 2) -or $secs -ge 40) {
                    $why = if ($ended) { "the check ended without an answer (exit $($script:DnsProc.ExitCode))" } else { "no answer after 40 s" }
                    Write-ScopsLog "DNS check try $($script:DnsTries): $why"
                    if (-not $ended) { try { & taskkill /F /T /PID $script:DnsProc.Id 2>&1 | Out-Null } catch {} }
                    $script:DnsTimer.Stop()
                    if ($script:DnsTries -lt 2) {
                        $dnsDetail.Text = "No answer yet, checking again..."
                        Start-ScopsDns
                    } else {
                        $script:DnsState = "warn"
                        $dnsHead.Text = "DNS check gave no answer"
                        $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(160, 100, 0)
                        $dnsDetail.Text = "Press Check again. You can still put 38.49.212.120 in the PS2."
                        if ($ended -and $script:DnsProc.ExitCode -eq 9009) {   # cmd.exe: program not found
                            $dnsHead.Text = "DNS check needs Python 3"
                            $dnsDetail.Text = "The check program could not start (exit 9009). Install Python 3 from python.org (tick 'Add python.exe to PATH'), then press Check again. You can still put 38.49.212.120 in the PS2."
                        }
                        $dnsRetry.Visible = $true
                        $dnsIcon.Invalidate()
                    }
                    return
                }
            }
            if ($script:DnsProc -and $script:DnsProc.HasExited -and -not $script:DnsExitLogged -and $script:DnsMode -eq "serve") {
                $script:DnsExitLogged = $true
                Write-ScopsLog "DNS exited code $($script:DnsProc.ExitCode)"
                if ($script:DnsState -eq "busy") {
                    $script:DnsState = "bad"
                    $dnsHead.Text = "DNS stopped"
                    $dnsHead.ForeColor = [System.Drawing.Color]::FromArgb(180, 25, 35)
                    $dnsDetail.Text = "The DNS server exited (code $($script:DnsProc.ExitCode)). See scops-dns.log in this folder."
                    $dnsIcon.Invalidate()
                }
            }
        } catch {
            Write-ScopsLog "timer error: $($_.Exception.Message)"
        }
    })
    $script:DnsTimer.Start()
    Write-ScopsLog "DNS reader started"
}


$dnsRetry.Add_Click({
    $dnsRetry.Visible = $false
    $script:DnsTries = 0
    $dnsHead.Text = "Checking DNS..."
    $dnsHead.ForeColor = [System.Drawing.SystemColors]::ControlText
    $dnsDetail.Text = "Checking whether your provider rewrites DNS."
    $dnsIcon.Invalidate()
    if ($script:DnsTimer) { try { $script:DnsTimer.Stop() } catch {} }
    try { Start-ScopsDns } catch { Write-ScopsLog "Start-ScopsDns failed: $($_.Exception.Message)" }
})

$dnsBtn.Add_Click({
    $dnsBtn.Enabled = $false
    $dnsBtn.Text = "Starting..."
    $script:DnsState = "busy"
    $dnsHead.Text = "Starting local DNS..."
    $dnsHead.ForeColor = [System.Drawing.SystemColors]::ControlText
    $dnsIcon.Invalidate()
    $script:DnsMode = "serve"
    $script:DnsExitLogged = $false
    if ($script:DnsProc -and -not $script:DnsProc.HasExited) {
        try { & taskkill /F /T /PID $script:DnsProc.Id 2>&1 | Out-Null } catch {}
    }
    $dir = Split-Path -Parent $PSCommandPath
    $exe = $null; $arg = $null
    $tool = Get-DnsTool "serve"
    if ($tool) { $exe = $tool.Exe; $arg = $tool.Arg; $script:DnsToolKind = $tool.Kind }
    $picked = Get-PickedIp
    if ($exe -and $picked) { $arg += " $($tool.IpFlag) $picked" }
    if (-not $exe) { $dnsDetail.Text = "scopsDNS.ps1 is missing from this folder. Unzip the whole SCOPS folder again and try again."; return }
    $dnsLog = Join-Path $dir "scops-dns.log"
    if (Test-Path $dnsLog) { Remove-Item $dnsLog -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType File -Path $dnsLog -Force | Out-Null
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "cmd.exe"
    $psi.Arguments = "/c `"`"$exe`" $arg > `"$dnsLog`" 2>&1`""
    $psi.WorkingDirectory = $dir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1"
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    [void]$proc.Start()
    $script:DnsProc = $proc
    try { if ($script:DnsRead) { $script:DnsRead.Close() } } catch {}
    $fs = [System.IO.File]::Open($dnsLog, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $script:DnsRead = New-Object System.IO.StreamReader($fs)
    Write-ScopsLog "local DNS started pid $($proc.Id)"
})
try { Start-ScopsDns } catch { Write-ScopsLog "Start-ScopsDns failed: $($_.Exception.Message)" }
[void]$form.ShowDialog()
Write-ScopsLog "window closed"
try { $script:OneWindow.ReleaseMutex() } catch {}
