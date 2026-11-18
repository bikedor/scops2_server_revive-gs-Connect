# How the beta tests were run

Home test setup used for the SCOPS beta sessions (Dorian's own two consoles), so results can be compared with
other players' setups.

## PCSX2 (account COOLDOR)
| | |
|---|---|
| PC | Windows 11, AMD Ryzen 9 9950X3D, wired Ethernet |
| Emulator | PCSX2 nightly build (logs report v2.8.2), software renderer (closest to real PS2) |
| Game | Splinter Cell Chaos Theory NTSC-U, SLUS-21137 (online executable SC3_ON.ELF) |
| Network mode | **Sockets** or **pcap bridged** (Ethernet), noted per session |
| Router | ASUS router between the PC and the ISP modem; PC at 192.168.1.113 (fixed) |
| Port forward | **pcap bridged:** always forwarded (UDP 3658 and 10070-10080 -> the emulated PS2's own LAN address). **Sockets:** ASUS rule `SCCT-GAME` (UDP 3658 and 10070-10080 -> the PC, 192.168.1.113) added on 2026-11-13 ~06:00 server time. Before that, Sockets ran with no forward, which explains the 2026-11-12 and early 2026-11-13 failures; with it, Sockets hosting worked. |
| pcap bridged | The emulated PS2 gets its own LAN address (e.g. 192.168.1.70), so its forward points at that address; in Sockets mode the game uses the PC's address, so the forward must point at the PC |

## Real PS2 (account DORIAN_D)
| | |
|---|---|
| Console | Fat PS2 (mainly, lately) or slim, official network adapter |
| Connection | Directly to the ISP modem, no router: its own public IP, nothing filtered ("just works") |

## Internet
- ISP gives public IPv4 addresses, **no CGNAT**. The PC (behind the ASUS) and the PS2 (on the modem) have different
  public IPs, so the server sees them as two separate homes.
- Server: beta on the VPS (38.49.212.120), relay ports UDP 45000-45012.

## How a test session goes
1. Start the beta with `BETA Server.bat`; check the start lines of `logs\udp_45000_reply.log` for the features on.
2. One console hosts (room, revenge on), the other joins; then swap hosts.
3. Play several maps with revenge (team swap) and returns to the lobby; leave and rejoin the room several times.
4. Note times of anything odd (error text on screen, freezes, failed joins).
5. Collect `udp_45000_reply.log`, `router_wm_40005.log`, `router_40000.log`, the PCSX2 log, and when useful a
   Wireshark capture on the PC (filter `udp and host 38.49.212.120`).

Why it mattered: pcap bridged was the most stable mode because its ports were always forwarded. Sockets mode had
no forward, so the ASUS dropped relay packets on some relay ports (Wireshark, 2026-11-13 04:30). Adding the forward to
the PC fixed it.

## Results so far
| Date (server time) | Setup | Result |
|---|---|---|
| 2026-11-11 | PS2 host (modem), outside players joining | SRP wrap proven; join-port bug found (Strider) |
| 2026-11-12 | PCSX2 Sockets host, no port forward | Joiner lost at a revenge swap (Mount Hospital) |
| 2026-11-13 03:58-04:07 | PCSX2 Sockets host, no port forward | Joiner lost after a revenge swap; host's packets on one relay port lost |
| 2026-11-13 04:26-04:32 | Same, Wireshark capture | Router dropped relay packets on one port; return to lobby and rejoins failed |
| 2026-11-13 04:47-05:30 | PCSX2 Sockets host, **port forward on** | 40 min, all maps with revenge, no drops; ack wrapped 4x. One rejoin failed and the PS2 froze (relay leave bug) |
| 2026-11-13 06:04-06:40 | Leave-grace update; PCSX2 Sockets + port forward, then PS2 hosting | 12 matches, 23 of 23 joins, 21 leaves handled, no freezes |
| 2026-11-13 22:29 - 2026-11-14 00:36 | First 4-player session: Dorian_D (PS2, modem), Snake (PCSX2 pcap, streaming to YouTube), StandaloneF0X, Wickedness; account + game check deployed | 25 failed joins. 21 in rooms hosted by Snake, Fox, Wickedness (routers that change ports): the relay dropped the host's session, so the second or third joiner failed. 3 in Dorian_D's rooms: a join was given another player's hole. 1 during a map load. Fixes for the first two in the next update. Snake dropped at map changes 4 times, only him; cause not established (his PCSX2 log shows thousands of dropped jumbo frames; he was streaming) |
