# scopsDNS.ps1 - built-in copy of scopsDNS.py for the SCOPS window (V8.15). Needs nothing but Windows PowerShell 5.1.
# Same checks, same verdicts and the same output lines as scopsDNS.py 2.1, so the window reads it the same way:
#   -CheckOnly : test whether this network lets the PS2 use the SCOPS DNS (38.49.212.120), then exit.
#   (default)  : local DNS for a network that hijacks DNS: game names -> the SCOPS server, everything else forwarded.
# The window uses scopsDNS.exe or a real Python with scopsDNS.py when there is one, and this file otherwise.
param(
    [switch]$CheckOnly,
    [switch]$NoFirewall,
    [switch]$NoUpnp,
    [string]$Ip = "",
    [string]$Server = "38.49.212.120",
    [string[]]$Upstream = @("1.1.1.1", "8.8.8.8", "9.9.9.9")
)

$ErrorActionPreference = "Stop"
$VERSION = "2.1-ps"
$GAME_SUFFIXES = @("ubisoft.com", "ubi.com", "gamespy.com", "gamespy.net", "dnas.playstation.org")
$GAME_NAMES = @("noname")
$CANARY_IP = "192.0.2.1"
$TTL = 60
$RULE = "scopsDNS"
$Out = [Console]::Out

function Say([string]$level, [string]$msg) {
    $Out.WriteLine(("[{0}] [{1}] {2}" -f (Get-Date -Format "HH:mm:ss"), $level, $msg))
    $Out.Flush()
}
function P([string]$line = "") { $Out.WriteLine($line); $Out.Flush() }

# ------------------------------------------------------------------------------------------- addresses
function Get-MainIp {
    foreach ($target in @("8.8.8.8", "10.255.255.255")) {
        $s = $null
        try {
            $s = New-Object System.Net.Sockets.Socket([System.Net.Sockets.AddressFamily]::InterNetwork,
                [System.Net.Sockets.SocketType]::Dgram, [System.Net.Sockets.ProtocolType]::Udp)
            $s.Connect($target, 1)
            $a = $s.LocalEndPoint.Address.ToString()
            if ($a -and -not $a.StartsWith("0.") -and -not $a.StartsWith("127.")) { return $a }
        } catch {
        } finally {
            if ($s) { $s.Close() }
        }
    }
    return "127.0.0.1"
}

function Get-AllIps {
    $found = @()
    try {
        foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            if ($nic.OperationalStatus -ne "Up") { continue }
            foreach ($u in $nic.GetIPProperties().UnicastAddresses) {
                if ($u.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
                    $a = $u.Address.ToString()
                    if (-not $a.StartsWith("127.") -and -not $a.StartsWith("169.254.")) { $found += $a }
                }
            }
        }
    } catch {}
    return @($found | Sort-Object -Unique)
}

function Format-PsIp([string]$address) {
    return (($address.Split(".") | ForEach-Object { "{0:D3}" -f [int]$_ }) -join ".")
}

function Test-PrivateIp([string]$a) {
    # Python's ipaddress is_private for the ranges that matter here
    $o = $a.Split(".")
    if ($o.Count -ne 4) { return $false }
    try { $b = $o | ForEach-Object { [int]$_ } } catch { return $false }
    if ($b[0] -eq 10 -or $b[0] -eq 127 -or $b[0] -eq 0) { return $true }
    if ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) { return $true }
    if ($b[0] -eq 192 -and $b[1] -eq 168) { return $true }
    if ($b[0] -eq 169 -and $b[1] -eq 254) { return $true }
    if ($b[0] -eq 192 -and $b[1] -eq 0 -and ($b[2] -eq 2 -or $b[2] -eq 0)) { return $true }
    if ($b[0] -eq 198 -and ($b[1] -eq 18 -or $b[1] -eq 19)) { return $true }
    if ($b[0] -eq 198 -and $b[1] -eq 51 -and $b[2] -eq 100) { return $true }
    if ($b[0] -eq 203 -and $b[1] -eq 0 -and $b[2] -eq 113) { return $true }
    if ($b[0] -ge 240) { return $true }
    return $false
}

# ------------------------------------------------------------------------------------------- DNS packets
function U16([byte[]]$d, [int]$o) { return ([int]$d[$o] -shl 8) -bor [int]$d[$o + 1] }

function New-DnsQuery([string]$name, [int]$qtype = 1, [int]$qclass = 1) {
    $b = New-Object System.Collections.Generic.List[byte]
    $tid = Get-Random -Maximum 65536
    $b.Add([byte]($tid -shr 8)); $b.Add([byte]($tid -band 255))
    foreach ($x in @(0x01, 0x00, 0, 1, 0, 0, 0, 0, 0, 0)) { $b.Add([byte]$x) }
    foreach ($part in $name.Split(".")) {
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($part)
        $b.Add([byte]$bytes.Length); $b.AddRange($bytes)
    }
    $b.Add(0)
    $b.Add([byte]($qtype -shr 8)); $b.Add([byte]($qtype -band 255))
    $b.Add([byte]($qclass -shr 8)); $b.Add([byte]($qclass -band 255))
    return ,$b.ToArray()
}

function Skip-Name([byte[]]$r, [int]$off) {
    while ($true) {
        if ($off -ge $r.Length) { throw "short" }
        $n = [int]$r[$off]
        if ($n -eq 0) { return $off + 1 }
        if (($n -band 0xC0) -eq 0xC0) { return $off + 2 }
        $off += 1 + $n
    }
}

function Get-Records([byte[]]$r) {
    # A (IPv4) and TXT answers; stops quietly at a broken packet
    $a = @(); $txt = @()
    try {
        $qd = U16 $r 4; $an = U16 $r 6; $off = 12
        for ($i = 0; $i -lt $qd; $i++) { $off = (Skip-Name $r $off) + 4 }
        for ($i = 0; $i -lt $an; $i++) {
            $off = Skip-Name $r $off
            $rtype = U16 $r $off; $rdlen = U16 $r ($off + 8); $off += 10
            if ($rtype -eq 1 -and $rdlen -eq 4) { $a += ("{0}.{1}.{2}.{3}" -f $r[$off], $r[$off + 1], $r[$off + 2], $r[$off + 3]) }
            if ($rtype -eq 16 -and $rdlen -gt 0) {
                $ln = [int]$r[$off]
                $txt += [System.Text.Encoding]::ASCII.GetString($r, $off + 1, [Math]::Min($ln, $r.Length - $off - 1))
            }
            $off += $rdlen
        }
    } catch {}
    return @{ A = $a; Txt = $txt }
}

function Invoke-Probe([string]$server, [string]$name, [int]$qtype = 1, [int]$qclass = 1, [double]$timeout = 2.5) {
    $fail = @{ ok = $false; src = ""; rcode = -1; answers = @(); txt = @(); local = $false }
    $q = New-DnsQuery $name $qtype $qclass
    $u = $null
    try {
        $u = New-Object System.Net.Sockets.UdpClient(0)
        $u.Client.ReceiveTimeout = [int]($timeout * 1000)
        [void]$u.Send($q, $q.Length, $server, 53)
        $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
        $resp = $u.Receive([ref]$ep)
    } catch {
        return $fail
    } finally {
        if ($u) { $u.Close() }
    }
    $src = $ep.Address.ToString()
    if ($resp.Length -lt 12 -or $resp[0] -ne $q[0] -or $resp[1] -ne $q[1]) { $fail.src = $src; return $fail }
    $flags = U16 $resp 2
    $rec = Get-Records $resp
    return @{ ok = $true; src = $src; rcode = ($flags -band 0x000F); answers = @($rec.A); txt = @($rec.Txt);
              local = ((Test-PrivateIp $src) -or $src.StartsWith("127.")) }
}

function Get-Who([string]$server) {
    foreach ($n in @("version.bind", "id.server")) {
        $r = Invoke-Probe $server $n 16 3 1.5
        if ($r.ok -and $r.txt.Count -gt 0) { $t = [string]$r.txt[0]; return $t.Substring(0, [Math]::Min(60, $t.Length)) }
    }
    return ""
}

function Get-SrcKind([string]$src, [string]$server, $own) {
    if (-not $src) { return "" }
    if ($src.StartsWith("127.") -or ($own -contains $src)) { return "pc" }
    if ($src -eq $server) { return "server" }
    if (Test-PrivateIp $src) { return "router" }
    return "remote"
}

function Get-DnsVerdict([string]$server, $wrong, [bool]$canaryOk, $fake, [string]$who, $own) {
    $by = ""; if ($who) { $by = " (it calls itself '$who')" }
    foreach ($w in $wrong) { $w.kind = Get-SrcKind $w.src $server $own }
    $fakeOk = [bool]$fake.ok; $fakeR = $fake.rcode; $fakeSrc = [string]$fake.src
    $subst = $fakeOk -and $fakeR -ne 5
    $fakeKind = ""; if ($subst) { $fakeKind = Get-SrcKind $fakeSrc $server $own }
    $refused = $fakeOk -and $fakeR -eq 5 -and $fakeSrc -eq $server
    $remote = @($wrong | Where-Object { $_.kind -eq "remote" })
    $router = @($wrong | Where-Object { $_.kind -eq "router" })
    if ($remote.Count -or $router.Count -or $fakeKind -eq "remote" -or $fakeKind -eq "router") {
        if ($router.Count -or $fakeKind -eq "router") {
            $s = if ($router.Count) { $router[0].src } else { $fakeSrc }
            $reason = "your router ($s) answers DNS itself$by"
        } else {
            $s = if ($remote.Count) { $remote[0].src } else { $fakeSrc }
            $reason = "questions sent to $server are answered by another DNS server ($s)$by"
        }
        $detail = if ($wrong.Count) { ($wrong | ForEach-Object { $_.text }) -join "; " } else { "a test name came back from $fakeSrc" }
        return @("ISP DNS hijack detected: $reason, not $server.", "detail: $detail",
                 "A PS2 set straight to the SCOPS DNS will not work from this network. Use the local DNS.")
    }
    $pc = @($wrong | Where-Object { $_.kind -eq "pc" })
    $forged = @($wrong | Where-Object { $_.kind -eq "server" })
    if ($canaryOk -or $pc.Count -or $forged.Count -or $fakeKind -eq "pc" -or $fakeKind -eq "server") {
        if ($pc.Count) { $where = $pc[0].text }
        elseif ($forged.Count) { $where = $forged[0].text.Replace("from $server", "in the name of $server") }
        elseif ($canaryOk) { $where = "a question sent to 192.0.2.1, where no DNS server exists, was answered" }
        else { $where = "a test name came back with code $fakeR" }
        $extra = ""; if ($refused) { $extra = " The SCOPS server itself refused a test name, so it is reachable." }
        return @("PC DNS filter: DNS on this PC or your network is answered by something else ($where)$by.$extra",
                 "Put $server in the PS2 first. If the game does not get online (DNAS error), use the local DNS.")
    }
    if ($refused) {
        return @("Could not get a SCOPS answer from $server, but the server itself is reachable (it refused a test name as expected). Try again in a minute.")
    }
    return @("Could not get a SCOPS answer from $server (no reply).",
             "Use the local DNS. The SCOPS DNS may be blocked on this network even if public DNS works.")
}

function Test-DnsHijack([string]$server) {
    $wrong = @()
    foreach ($name in @("gsconnect.ubisoft.com", "gate1.us.dnas.playstation.org", "gsconnect.ubisoft.com")) {
        $r = Invoke-Probe $server $name
        if ($r.ok -and ($r.answers -contains $server) -and $r.src -eq $server) {
            Say "INFO" "SCOPS DNS $server answered $name with $server."
            Say "INFO" "no hijack detected: your ISP lets you use the SCOPS DNS directly."
            return
        }
        if ($r.ok -and -not ($r.rcode -eq 5 -and $r.src -eq $server)) {
            $got = if ($r.answers.Count) { $r.answers -join ", " } else { "no address (code $($r.rcode))" }
            $where = if ($r.local) { "this PC or your home network" } else { $r.src }
            $wrong += @{ text = "$name came back as $got from $where"; local = $r.local; src = $r.src; kind = "" }
        }
        Start-Sleep -Milliseconds 300
    }
    $canary = Invoke-Probe $CANARY_IP "gsconnect.ubisoft.com" 1 1 1.5
    $madeUp = "scops-check-{0:x8}.invalid" -f (Get-Random)
    $fake = Invoke-Probe $server $madeUp 1 1 1.5
    $interesting = $canary.ok -or $wrong.Count -or ($fake.ok -and $fake.rcode -ne 5)
    $who = ""; if ($interesting) { $who = Get-Who $server }
    $c = if ($canary.ok) { "answered" } else { "silent (good)" }
    $f = if ($fake.ok -and $fake.rcode -ne 5) { "answered, code $($fake.rcode)" } elseif ($fake.ok) { "refused (good)" } else { "no reply" }
    $w = ""; if ($who) { $w = "; answering server calls itself '$who'" }
    Say "INFO" "checks: 192.0.2.1 $c; made-up name $f$w"
    $own = @(Get-AllIps) + @(Get-MainIp)
    foreach ($line in (Get-DnsVerdict $server $wrong ([bool]$canary.ok) $fake $who $own)) { Say "WARN" $line }
}

# ------------------------------------------------------------------------------------------- local DNS
function Get-Question([byte[]]$d) {
    if ($d.Length -lt 12) { return $null }
    $flags = U16 $d 2; $qd = U16 $d 4
    if (($flags -band 0x8000) -or $qd -ne 1) { return $null }
    $labels = @(); $off = 12
    while ($true) {
        if ($off -ge $d.Length) { return $null }
        $n = [int]$d[$off]; $off++
        if ($n -eq 0) { break }
        if (($n -band 0xC0) -or $off + $n -gt $d.Length) { return $null }
        $labels += [System.Text.Encoding]::ASCII.GetString($d, $off, $n); $off += $n
    }
    if ($off + 4 -gt $d.Length) { return $null }
    return @{ name = ($labels -join ".").ToLower(); qtype = (U16 $d $off); qend = $off + 4 }
}

function Test-GameName([string]$name) {
    $name = $name.TrimEnd(".")
    if ($GAME_NAMES -contains $name) { return $true }
    foreach ($s in $GAME_SUFFIXES) { if ($name -eq $s -or $name.EndsWith("." + $s)) { return $true } }
    return $false
}

function New-GameAnswer([byte[]]$q, [int]$qend, [int]$qtype, [string]$ip) {
    $rd = (U16 $q 2) -band 0x0100
    $flags = 0x8000 -bor 0x0400 -bor $rd -bor 0x0080
    $isA = ($qtype -eq 1 -or $qtype -eq 255)
    $b = New-Object System.Collections.Generic.List[byte]
    $b.Add($q[0]); $b.Add($q[1])
    $b.Add([byte]($flags -shr 8)); $b.Add([byte]($flags -band 255))
    foreach ($x in @(0, 1, 0, ([int]$isA), 0, 0, 0, 0)) { $b.Add([byte]$x) }
    for ($i = 12; $i -lt $qend; $i++) { $b.Add($q[$i]) }
    if ($isA) {
        foreach ($x in @(0xC0, 0x0C, 0, 1, 0, 1, 0, 0, 0, $TTL, 0, 4)) { $b.Add([byte]$x) }
        foreach ($o in $ip.Split(".")) { $b.Add([byte][int]$o) }
    }
    return ,$b.ToArray()
}

function New-ServFail([byte[]]$q) {
    $qq = Get-Question $q
    $rd = (U16 $q 2) -band 0x0100
    $flags = 0x8000 -bor $rd -bor 0x0080 -bor 2
    $b = New-Object System.Collections.Generic.List[byte]
    $b.Add($q[0]); $b.Add($q[1]); $b.Add([byte]($flags -shr 8)); $b.Add([byte]($flags -band 255))
    $has = [int]($null -ne $qq)
    foreach ($x in @(0, $has, 0, 0, 0, 0, 0, 0)) { $b.Add([byte]$x) }
    if ($qq) { for ($i = 12; $i -lt $qq.qend; $i++) { $b.Add($q[$i]) } }
    return ,$b.ToArray()
}

function Send-Upstream([byte[]]$q) {
    foreach ($up in $Upstream) {
        $u = $null
        try {
            $u = New-Object System.Net.Sockets.UdpClient(0)
            $u.Client.ReceiveTimeout = 2500
            [void]$u.Send($q, $q.Length, $up, 53)
            $deadline = (Get-Date).AddMilliseconds(2500)
            while ((Get-Date) -lt $deadline) {
                $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
                $r = $u.Receive([ref]$ep)
                if ($ep.Address.ToString() -eq $up -and $r.Length -ge 2 -and $r[0] -eq $q[0] -and $r[1] -eq $q[1]) { return ,$r }
            }
        } catch {
        } finally {
            if ($u) { $u.Close() }
        }
    }
    return $null
}

function Open-Firewall {
    $cmds = @()
    foreach ($pair in @(@("DNS-UDP", "UDP"), @("DNS-TCP", "TCP"))) {
        $n = "$RULE-$($pair[0])"
        $cmds += "netsh advfirewall firewall delete rule name=$n"
        $cmds += "netsh advfirewall firewall add rule name=$n dir=in action=allow protocol=$($pair[1]) localport=53 profile=private,domain"
    }
    Say "INFO" "Opening firewall rules on Windows for DNS (UDP/TCP 53)."
    Say "INFO" "Windows will ask for administrator permission to change the firewall."
    try {
        Start-Process -FilePath "cmd.exe" -ArgumentList ("/c " + ($cmds -join " & ")) -Verb RunAs -WindowStyle Hidden -Wait
    } catch {
        Say "ERROR" "Firewall not changed (permission was refused). You can also allow it when Windows asks."
        return
    }
    $chk = & netsh advfirewall firewall show rule "name=$RULE-DNS-UDP" 2>$null
    if ($LASTEXITCODE -eq 0 -and ($chk -join " ") -match "53") { Say "INFO" "Firewall rules are in place." }
    else { Say "INFO" "Could not confirm the firewall rules." }
}

function Show-Banner([string]$ip) {
    P "+===================================+"
    P "|          SCOPS DNS Server         |"
    P "|          Version $VERSION           |"
    P "+===================================+"
    P ""
    P "== Welcome to scopsDNS! =="
    P "Splinter Cell Chaos Theory (PS2) online. DNS for a blocked provider (built-in Windows version, no Python needed)."
    P ""
    P "Put these settings in for DNS on your PS2 / in PCSX2:"
    P "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
    P ("Primary DNS:   " + (Format-PsIp $ip))
    P ("Secondary DNS: " + (Format-PsIp $ip))
    P "~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
    P "(Use this computer for both. A public DNS as secondary would send the game to Ubisoft's old servers.)"
    $others = @(Get-AllIps | Where-Object { $_ -ne $ip })
    if ($ip -eq "127.0.0.1") { P "WARNING: no network connection found. Connect this computer to your home network and restart scopsDNS." }
    elseif ($others.Count) {
        P "This computer has more than one network address. If the console is on a different one, use that instead:"
        P ("    " + (($others | ForEach-Object { Format-PsIp $_ }) -join ", "))
    }
    P ""
    P ("Game lookups are answered with the SCOPS server $Server; everything else goes to " + ($Upstream -join ", ") + ".")
    P ""
}

function Start-LocalDns([string]$ip) {
    $listen = [System.Net.IPAddress]::Any
    $u = $null
    try {
        $bindAll = New-Object System.Net.IPEndPoint($listen, 53)
        $u = New-Object System.Net.Sockets.UdpClient($bindAll)
    } catch {
        $u = $null
        if ($ip -ne "127.0.0.1") {
            try {
                $bindOne = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Parse($ip), 53)
                $u = New-Object System.Net.Sockets.UdpClient($bindOne)
                Say "INFO" "Port 53 is busy on all addresses; listening on $ip only."
            } catch { $u = $null }
        }
    }
    try {   # Windows: a reply to a console that has gone away must not make the next receive fail (SIO_UDP_CONNRESET)
        [void]$u.Client.IOControl(-1744830452, [byte[]](0, 0, 0, 0), $null)
    } catch {}
    if (-not $u) {
        Say "ERROR" "Cannot listen on UDP port 53."
        Say "ERROR" "Port 53 is already in use, or this program is not allowed to take it."
        Say "ERROR" "Close any other scopsDNS window, then open this one again. Another program may use port 53 (often 'Internet Connection Sharing')."
        exit 1
    }
    Say "INFO" "TCP port 53 not used by the built-in version; UDP only (enough for the PS2)."
    Say "INFO" "scopsDNS is ready. Waiting for DNS requests from your console... (Ctrl+C to stop)"
    $script:SeenConsoles = @{}
    while ($true) {
        try {
            $ep = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, 0)
            $data = $u.Receive([ref]$ep)
        } catch { Start-Sleep -Milliseconds 50; continue }
        try {
            $client = $ep.Address.ToString()
            $q = Get-Question $data
            if (-not $q) { Say "ERROR" "Invalid DNS request from $client"; continue }
            if (Test-GameName $q.name) {
                if (-not $script:SeenConsoles.ContainsKey($client)) {
                    $script:SeenConsoles[$client] = $true
                    Say "INFO" "PS2 address seen: $client (it asked this computer for $($q.name); use it for hosting)"
                }
                if ($q.qtype -eq 1 -or $q.qtype -eq 255) { Say "INFO" "Console at $client looked up $($q.name) -> $Server (SCOPS)" }
                $reply = New-GameAnswer $data $q.qend $q.qtype $Server
            } else {
                $reply = Send-Upstream $data
                if (-not $reply) { $reply = New-ServFail $data }
            }
            [void]$u.Send($reply, $reply.Length, $ep)
        } catch {
            Say "WARN" "request failed: $($_.Exception.Message)"
        }
    }
}

# ------------------------------------------------------------------------------------------------ main
$ipMain = Get-MainIp
if ($Ip) {
    $mine = @(Get-AllIps) + @($ipMain)
    if ($mine -contains $Ip) { $ipMain = $Ip }
    else { Say "WARN" ("$Ip is not an address of this computer (it has " + (($mine | Sort-Object -Unique) -join ", ") + "); showing $ipMain instead.") }
}
if ($CheckOnly) {
    Say "INFO" ("This computer's address: " + (Format-PsIp $ipMain))
    Test-DnsHijack $Server
    exit 0
}
Show-Banner $ipMain
Say "INFO" "Detected operating system: Windows"
if (-not $NoFirewall) { Open-Firewall }
else { Say "INFO" "Firewall rules not changed (--no-firewall). If the PS2 cannot reach this computer, allow it on Private networks when Windows asks." }
try {
    $t = New-Object System.Net.Sockets.TcpClient
    $iar = $t.BeginConnect($Server, 40000, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(4000) -and $t.Connected) { Say "INFO" "SCOPS server $Server is reachable (TCP 40000)." }
    else { Say "WARN" "Could not reach the SCOPS server $Server on TCP 40000."; Say "WARN" "The server may be down, or something on your network blocks it." }
    $t.Close()
} catch { Say "WARN" "Could not reach the SCOPS server $Server on TCP 40000 ($($_.Exception.Message))." }
Test-DnsHijack $Server
Start-LocalDns $ipMain
