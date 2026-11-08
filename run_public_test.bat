@echo off
rem Hosted test from the home PC: outside consoles are told the public IP, LAN consoles keep the LAN IP.
rem 1) forward the ports on the router (not DMZ)  2) run this  3) wait ~15 s, then dnasrep_test\run_dnasrep_443_only.bat
rem Edit SCCT_PUBLIC_IP if your public IP changes.
rem SCCT_NAT_PUBLIC_ADDR=1: the NAT service reports the PUBLIC address for LAN players too, so rooms hosted here can be
rem joined from outside. LAN-only play then goes through the router (NAT loopback); remove it for pure LAN sessions.
set SCCT_ALLOW_PUBLIC=1
set SCCT_PUBLIC_IP=38.49.212.120
set SCCT_NAT_PUBLIC_ADDR=1
rem SCCT_RELAY=1: outside joiners behind a different NAT than the host are routed through this server (UDP relay on
rem 3658/10070-10080 -> host via 45000). Needs no port forwarding on the host's side. SCCT_RELAY_IP defaults to SCCT_PUBLIC_IP.
set SCCT_RELAY=1
rem Hex-dump the first N datagrams per direction of each relayed flow. Debugging only: it grows the log and records traffic.
set SCCT_RELAY_DUMP=0
rem Adaptive steering (off by default, not yet tried in a real session). The relay always logs "HOLE open-NAT evidence" when a
rem host answers on a hole it never pinged. With 1 it also stops spending steers on such a host, and spaces all steers out.
set SCCT_RELAY_ADAPTIVE_STEER=0
rem Multi-hole relay v3: three holes on the host's 3658 socket (45001-45003), one per joiner link. A hole is OPENED by a
rem steering PORT_ID (login + when a new link finds the host silent); once it has carried traffic both ways the relay keeps it
rem open with an empty datagram every 45 s (conntrack refresh) - no steering budget spent. Host sockets 10071-10073 do not
rem accept peer links (tested 18:53), so alt-socket mode is off.
rem 12 holes: each joiner uses 2-3 (probe+room link share one, game link one); a 4-player room needs up to 9. Firewall: 45000-45012.
set SCCT_RELAY_HOLES=45001,45002,45003,45004,45005,45006,45007,45008,45009,45010,45011,45012
set SCCT_RELAY_ALT_HOST_PORTS=0
call "%~dp0run_nat_even.bat"
