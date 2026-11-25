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
rem Hosts whose router lets outside players in (DMZ, or UDP 3658 + 10070-10080 forwarded): their rooms skip the relay. Names or IPs,
rem comma separated, e.g. set SCCT_DIRECT_HOSTS=DORIAN_D. Leave empty until the ports are verified. Not yet tried in a real session.
set SCCT_DIRECT_HOSTS=
rem Multi-hole relay v3: three holes on the host's 3658 socket (45001-45003), one per joiner link. A hole is OPENED by a
rem steering PORT_ID (login + when a new link finds the host silent); once it has carried traffic both ways the relay keeps it
rem open with an empty datagram every 45 s (conntrack refresh) - no steering budget spent. Host sockets 10071-10073 do not
rem accept peer links (tested 18:53), so alt-socket mode is off.
rem 12 holes: each joiner uses 2-3 (probe+room link share one, game link one); a 4-player room needs up to 9. Firewall: 45000-45012.
set SCCT_RELAY_HOLES=45001,45002,45003,45004,45005,45006,45007,45008,45009,45010,45011,45012
set SCCT_RELAY_ALT_HOST_PORTS=0
rem Warm holes: steer the host's 25 s ping round each joiner's least recently used relay hole, so reconnects at
rem match start, revenge and back-to-lobby find it still open. 0 = off.
set SCCT_RELAY_WARM_HOLES=1
rem Wake move: a joiner socket that reconnects after a map load onto a cold hole is moved to a warm free hole.
rem Off for the first test night; set to 1 once warm holes alone has been checked.
set SCCT_RELAY_WAKE_MOVE=0
rem Leave grace: a link still sending when a player leaves a room keeps its relay hole until it goes quiet, and a
rem join forgets the player's idle links so the join starts on one hole (the 2026-11-13 rejoin failure and PS2 freeze).
set SCCT_RELAY_LEAVE_GRACE=1
rem No-reply move: a link the host never answers (8 s, while the host answers others) moves to a working hole, and
rem holes that failed (no reply, or a steer that never got a ping) are avoided for 10 minutes. 0 = off.
set SCCT_RELAY_NOREPLY_MOVE=1
rem Rescue no-wait: when a link comes back on a dead hole and the relay steers the host's ping there AFTER the link was
rem already sending into it, do not wait 60 s for that ping (on a router with no forward it is blocked) - move the link
rem at once to a hole the host answered on lately. 2026-11-15 Cinema revenge swap: LINEBACKER waited on such a steer and
rem was booted to the lobby 3 s before it was declared failed. 0 = old behaviour.
set SCCT_RELAY_RESCUE_NOWAIT=1
rem SRP by session: a host behind a router that changes ports keeps its NAT session (and its hole steering) when
rem the first player joins. Without it every third joiner failed ("game no longer available", 2026-11-13).
set SCCT_RELAY_SRP_BY_SESSION=1
rem Reuse quiet: a new link never takes another player's idle hole while the host has sent there in the last
rem 15 s (the game's connection timeout). 0 = off.
set SCCT_RELAY_REUSE_OTHER_QUIET_S=15
rem Rebinding follow: at back to the lobby each player's game opens a new link, pauses ~8 s while loading and carries
rem on. LINEBACKER (PCSX2 Sockets over a VPN) twice came back from a NEW port, the relay gave it his old room link's
rem hole, the host saw another address and he was booted to the online lobby (2026-11-15 04:23:48, 05:20:26). Now the
rem new port keeps the paused link's hole. 0 = off.
set SCCT_RELAY_REBIND_FOLLOW=1
rem Owner keep: another player's link keeps its relay hole through the game's normal pauses (7-9 s map loads, and a
rem room link quiet for a whole match). 2026-11-15 05:38 Cinema revenge swap: DORIAN_D's room-link hole was given to
rem LINEBACKER 7 s into DORIAN_D's load pause and DORIAN_D was dropped from the game. 0 = off.
set SCCT_RELAY_OWNER_KEEP=1
rem Own-socket keep: a player's socket that has come back after a long silence keeps its hole against that player's
rem other sockets. 2026-11-15 17:25 Krauser: DORIAN_D's PS2 socket 10073 was put on the hole of his socket 10071; at
rem the revenge swap 10071 came back elsewhere, the host dropped the shared hole and DORIAN_D was booted. 0 = off.
set SCCT_RELAY_OWN_SOCKET_KEEP=1
rem How long a paused socket keeps its hole against the player's other sockets: a whole map (two 8-12 min halves).
set SCCT_RELAY_OWN_SOCKET_KEEP_S=1800
rem Hold until pinged: on a host whose router has no port forward, relay packets that reach a new hole before the
rem host's ping block that ping (2026-11-14: COOLDOR hosting, a second joiner never got in). The relay then holds a
rem joiner on a cold hole until the host's ping opens it. auto = each host is tested on its first such link (hosts
rem that answer at once, e.g. a forward or a PS2 on the modem, are never held); on = hold for every host; off = old.
set SCCT_RELAY_HOLD_UNPINGED=auto
rem Resume ignore: an answer that comes only in the host's first burst after a map load does not prove an open NAT
rem (after a load the host's game sends to every player first). 2026-11-15: COOLDOR, forward and UPnP off, was judged
rem open twice exactly that way, so holding was off for those sessions. 0 = old behaviour.
set SCCT_RELAY_HOLD_RESUME_IGNORE=1
rem Downgrade: a host judged open is judged strict after all once one of its holes goes unanswered while it answers
rem others. 2026-11-15 06:00-06:20: COOLDOR (forward and UPnP off) answered a fresh hole at once and was judged open,
rem nothing was held, and at the Mount Hospital revenge swap DORIAN_D and ANDROID-DOR were dropped. 0 = off.
set SCCT_RELAY_HOLD_DOWNGRADE=1
rem No-reply verdict: a host not yet tested is judged strict when a link gets no reply on a hole that was dead when
rem it started (no game data 60 s, no ping 28 s) while the host answers others. 2026-11-15 16:02-16:14: COOLDOR stayed
rem untested 12 minutes; at the 16:08 Cinema revenge swap DORIAN_D's room link waited 27 s on such a hole. 0 = off.
set SCCT_RELAY_HOLD_NOREPLY_STRICT=1
rem Cold age on a strict host: a hole the host has not sent through for this many seconds counts as gone (was 170,
rem assuming a used mapping lives ~180 s). 2026-11-15 every COOLDOR session: a link back on a used hole after 70 s+
rem of host silence was never answered there, and its own packets then blocked the host's ping (06:40 Factory revenge
rem swap: ANDROID-DOR's room link, hole quiet 84 s, no reply for 65 s, spectator). Now such a link is held for the
rem host's ping or moved to a ready spare at once. 0 = old behaviour.
set SCCT_RELAY_HOLD_COLD_STRICT_S=60
rem AGE log (log only, changes no packet): one 'AGE' line per link that starts or comes back on a relay hole - how
rem long the host had been silent there, whether it was judged cold, any hold/move/spare, and how long until the host's
rem first packet reached the player (outcome=never after 70 s). It measures how long each host's router keeps a hole,
rem so the cold age can be set from data. Summary: python tools\sim\age_report.py logs\udp_45000_reply.log. 0 = off.
set SCCT_RELAY_AGE_LOG=1
rem Spare pool: on a host with no port forward (judged strict above) keep this many spare holes open between the
rem host's pings, so a player who reconnects at a map change does not always queue behind one ping per 25 s.
rem 2026-11-15 real test (COOLDOR, no forward, UPnP off): trusting a kept spare for longer the more it proved itself
rem backfired - a link was answered fine after 65-71s of silence, then another failed at just 39-42s. Keeping 3 spares
rem open at once also made a later stress test worse than no pool at all, by competing with players' own holes on a
rem small NAT table. Now: a fixed, modest ceiling (not grown from evidence) and only 1 spare kept at a time.
rem 0 = off (the relay then behaves exactly as hold v3).
set SCCT_RELAY_SPARE_POOL=1
rem Pool step-down: 2026-11-15 23:59 FOX: one spare silent 34s (just under the 35s ceiling) did not answer, and that
rem ONE miss switched the pool off for the rest of the night; the Cinema returns then waited 59-66s. Now the first
rem miss lowers the ceiling to 20s instead: the pool keeps steering the ping and moving returning links onto a fresh
rem hole, but stops relying on keepalives past a pinged hole's own life. 0 = the old rule (one miss = pool off).
set SCCT_RELAY_POOL_STEPDOWN=1
set SCCT_RELAY_POOL_TRUST_STEP_S=20
rem Shadow hole classes (stage 1, LOG ONLY - routing unchanged): 2026-11-16 COOLDOR with UPnP was judged strict
rem although fresh holes reached his PC; only a recently used hole (45001) was dead. These SHADOW lines keep evidence per
rem kind of hole (pinged / fresh / stale) and say what a fresh-hole rule WOULD do. Summary after a session:
rem   python tools\sim\shadow_report.py logs\udp_45000_reply.log
set SCCT_RELAY_SHADOW=1
rem scopsDNS 2.1 sends {"op":"upnp_open"} to 45000 from the host's PC. The relay used to pass it to the joiner's game as
rem game data (same public IP as the host). Now it is logged and dropped. 0 = old behaviour.
set SCCT_RELAY_UPNP_HINT=1
rem UPnP router probe (log only): the SCOPS window V7 forwards UDP 3659 to its PC after a green check and asks for a
rem test. The server sends small packets from ports 46001-46020 (outbound only, nothing to open on the VPS): two fresh,
rem one 45s after a two-way exchange (stale). Results: 'SHADOW probe result' lines. 0 = off.
set SCCT_RELAY_UPNP_PROBE=1
rem Failure steps (log only): 'NAT step' lines when a console socket stops at PORT_ID (no ping to 45000 in 10 s),
rem 'FAIL why' on every no-reply move (when the host last used the dead hole, which holes it was answering, the router
rem probe), avoided holes on AGE lines, and the evidence behind 'looks address-restricted'. 0 = off.
set SCCT_RELAY_FAIL_LOG=1
rem Stricter 'open' verdict. OFF by default (0 = exactly as before). 1 = an answer in the host's first burst after a
rem map load never counts as proof of open (whichever came first), and 2 clean answers are needed before a host is
rem called open. It can only keep a host unknown/strict longer; strict detection, holds and the spare are unchanged.
rem Turn on between sessions: set to 1 and restart the server.
set SCCT_RELAY_OPEN_STRICT=1
rem Relay guard (log only, nothing is dropped): per-link peak packets/s and largest packet, "GUARD would-limit" lines above
rem the limits below, a "GUARD peaks" summary every 10 min, and a count of unmapped senders. Data for a possible flood limit.
set SCCT_RELAY_GUARD_LOG=1
set SCCT_RELAY_GUARD_PPS=300
set SCCT_RELAY_GUARD_MAX_PKT=1472
rem Fair-handout shadow (log only, nothing changes): 'FAIR shadow' lines when a hole the relay kept warm for one player
rem goes to another player's link, what the owner then got, and a 'FAIR summary' every 10 min. Data for a possible
rem fair rule (warm holes only for the player they were warmed for).
set SCCT_RELAY_FAIR_SHADOW=1
rem Fast resume flag (log only, nothing changes): 'FAST RESUME' when the host answers a player's long-quiet link
rem within 1 s at a round end (before its map load), and a follow-up saying whether that player left the room in the
rem next 20 s. On 2026-11-19 the only two such fast answers (SNAKE 00:36, IY0 23:46) were both followed by a drop.
set SCCT_RELAY_FAST_RESUME_LOG=1
rem Watch links quiet 90 s or more (default 300): test rounds of 1-3 min leave links quiet only 107-260 s.
rem Over 407 round-end returns (2026-11-16..20) both limits find the same two fast answers, so 90 adds no noise.
set SCCT_RELAY_FAST_QUIET_S=90
rem UPnP-open shadow (log only): when a host is judged strict but its SCOPS window reported a working
rem UPnP forward and fresh router probes reached it, log that an open-host rule would keep it open.
rem Routing does not change. Set to 0 to turn the lines off.
set SCCT_RELAY_UPNP_OPEN_SHADOW=1
rem Pause shadow (log only): after a strict verdict, say whether the host was just pausing (sent nothing on
rem any other hole for 3s+, e.g. a match about to start) - then the verdict was a false alarm. Routing does not change.
set SCCT_RELAY_PAUSE_SHADOW=1
rem Pause-aware strict verdict (changes behaviour): before calling a host strict on a cold hole, wait up to 3s
rem for it to send to anyone else. Silent everywhere = a pause (e.g. match about to start), verdict cancelled
rem (max 2 per host). Set to 0 to go back to the old immediate verdict.
set SCCT_RELAY_PAUSE_AWARE=1
rem Loop timing (log only): every 5 min a LOOP STATS line - time relayed game packets spend in the server and in-loop pauses.
set SCCT_RELAY_LOOP_STATS=1
rem Ranked ladder (2026-11-20 captures): the room info's first byte has Game Type (bit 0x04 = Ranking) and mode
rem (low 2 bits: 3 Neutralisation, 2 Sabotage, 1 Extraction); the server follows every switch in the room menu.
rem Only record a match if the room was Ranking when it started (a room created Ranking and switched to Free
rem keeps sending scores, seen with STRIDERSTROKE 2026-11-18/19):
set SCCT_RANKED_REQUIRE_RANKED=1
rem Record each match under its mode from the mode bits (the old exact-byte check gave UNK for some hosts):
set SCCT_RANKED_MODE_BITS=1
rem Per-mode Ranking tabs (captures 2026-11-20: the Ranking screen's L1 tab is sent as 0 Overall, 1 Extraction,
rem 2 Sabotage, 3 Neutralisation). Keep Spy / Mercenary points per mode, and answer the Extraction, Sabotage and
rem Neutralisation tabs from them. The Overall tab and the end-of-match results screen are unchanged.
set SCCT_RANKED_MODE_SIDE_LEDGER=1
set SCCT_RANKED_MODE_TABS=1
rem 2026-11-23 tab test: the Game Statistics screen reads the played mode's tab on the player's side (as the original).
rem Answer each tab's "my stats" from that mode, and floor points at 0 per mode and side (a loss at 0 shows 0 -> 0).
set SCCT_RANKED_MYSTATS_TABS=1
set SCCT_RANKED_MODE_FLOOR=1
rem 2026-11-23 tests: when the host returns everyone to the lobby mid-match, every console sends all players as
rem lost and no Game Statistics screen appears. Such a match, where nobody won, is not counted. A player who quits on his own still
rem counts as a loss for his side, and the others get their win.
set SCCT_RANKED_REQUIRE_WINNER=1
rem 2026-11-24 player list test: the game shows the completed games percentage from GEN_GAMES and NB_DISC (Games List, Quick
rem Search, Player List). A player who quits a counted ranked match gets one disconnection.
set SCCT_RANKED_DISC=1
rem Game check at login: only these games may log in (product id prefixes, comma separated). SC3PS2 = Chaos
rem Theory, every region. Other Ubisoft PS2 games get the normal login failure. To host another game later, add it,
rem e.g. SC3PS2,PTPS2 for Pandora Tomorrow. SCCT_GAME_CHECK: refuse (default), log (let in, only log), off.
set SCCT_ALLOWED_GAMES=SC3PS2
set SCCT_GAME_CHECK=refuse
call "%~dp0run_nat_even.bat"
