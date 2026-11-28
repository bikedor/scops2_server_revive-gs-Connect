#!/usr/bin/env python3
"""GSNAT responder v3 for the SCCT server (replaces tools/udp_reply_server.py srp_nat mode).

v3 fix: the game batches up to 4 NAT address requests (one per presolved UDP socket: 3658, 10071, 10072, 10073)
into one SRP segment (data_size 208 = 4 x 52). v2 answered only the first, so the game waited 30 x 500 ms = 15 s in
ConnectThread (PresolvedSocketRequestInProgress) and then failed with "Cannot access Ubi.Com". v3 answers every request.

Follows the handshake documented by the newer ubi-gs (homm5/gsnat.py, verified there with HoMM5):

  1. game  -> SYN                      server -> SYN+ACK (window)
  2. game  -> NAT address request      server -> PORT_ID  [socketId, <random 32-bit ping id>, <this NAT port>]
  3. game  -> 4-byte ping (id, BE) from its own port to <this NAT port>
                                       server -> ADDRESS  [socketId, <ip seen in the ping>, <port seen>]
  4. later pings -> bare ACK (the game keeps unasked-for replies in a 536-byte receive buffer)

The old server answered step 2 with PORT_ID *and* ADDRESS at once, using the client's own source
port as the "NAT port", and mirrored every reply from a second port. This one does not.

Uses the newer ubi-gs modules in ../../ubi-gs-new (separate from the older ubi-gs the other tools use).
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import secrets
import select
import socket
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
NEW_UBIGS = None
for cand in (HERE.parent.parent / "ubi-gs-new", HERE.parent / "ubi-gs-new", HERE / "ubi-gs-new"):
    if (cand / "srp.py").is_file():
        NEW_UBIGS = cand
        break
if NEW_UBIGS is None:
    print("ERROR: ubi-gs-new folder not found next to tools/", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, str(NEW_UBIGS))

import client  # noqa: E402
import gsm  # noqa: E402
import srp  # noqa: E402
import utils  # noqa: E402

CLIENT_TIMEOUT = 150.0
# Gap between datagrams sent back to back (the 4 PORT_ID replies). A real PS2 has a small IOP/UDP receive path and may
# drop a burst that an emulator accepts. Set SCCT_NAT_GAP_MS=0 to send them all at once.
GAP_S = float(os.environ.get("SCCT_NAT_GAP_MS", "25")) / 1000.0
# Test option: answer only the first N NAT requests of a segment with a PORT_ID (0 = all). Used to find which reply
# makes a real PS2 hang. Unanswered requests are ignored, like the first server version ignored all but the first.
PORTID_LIMIT = int(os.environ.get("SCCT_NAT_PORTID_LIMIT", "0"))
# Test options for the real PS2, which answers a PORT_ID and pings within ~65 ms (PCSX2 needs ~450 ms):
#  SCCT_NAT_ADDRESS_DELAY_MS - wait this long after a ping before sending the ADDRESS reply
#  SCCT_NAT_NO_REPEAT_ACK=1  - ignore repeat pings instead of answering each with a bare ACK
ADDRESS_DELAY_S = float(os.environ.get("SCCT_NAT_ADDRESS_DELAY_MS", "0")) / 1000.0
NO_REPEAT_ACK = os.environ.get("SCCT_NAT_NO_REPEAT_ACK", "0") == "1"
#  SCCT_NAT_NO_ADDRESS=1     - never send the ADDRESS reply (tests whether the ADDRESS is what stalls the console)
NO_ADDRESS = os.environ.get("SCCT_NAT_NO_ADDRESS", "0") == "1"
# SCCT_NAT_EVEN (default on): every datagram sent to the game gets an EVEN length. On the real PS2 every run hung on the first
# odd-length datagram (45 B) and handled even ones (44/46 B); PCSX2 handles both. PORT_ID: pick a ping id that gives an
# even length. ADDRESS: if odd, write the port with a leading '+' (atoi/strtol/sscanf accept it, no length change otherwise).
EVEN_LEN = os.environ.get("SCCT_NAT_EVEN", "1") != "0"
PUBLIC_ADDR = os.environ.get("SCCT_NAT_PUBLIC_ADDR", "0") == "1"
PUBLIC_IP = os.environ.get("SCCT_PUBLIC_IP", "").strip()
CLIENTS: list = []
LOG_FP = None


def now_ts() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    line = f"[{now_ts()}] {msg}"
    print(line, flush=True)
    if LOG_FP is not None:
        LOG_FP.write(line + "\n")
        LOG_FP.flush()


def new_ping_id() -> int:
    while True:
        pid = secrets.randbits(32)
        if pid != 0 and find_by_ping(pid) is None:
            return pid


def find_by_ping(ping_id: int):
    """(client, request dict) for a NAT ping id, or None."""
    for c in CLIENTS:
        r = getattr(c, "reqs", {}).get(ping_id)
        if r is not None:
            return c, r
    return None


def nat_reply(req_msg, subtype, ip_or_id, port, pick_id=False):
    """NatResponse whose SRP datagram (12 B header + message) has an even length. Returns (response, ping_id_or_ip)."""
    for _ in range(64 if pick_id else 1):
        val = new_ping_id() if pick_id else ip_or_id
        nr = gsm.NatResponse(req_msg, subtype, val, port)
        if not EVEN_LEN or (srp.SRP_HEADER_SIZE + len(bytes(nr))) % 2 == 0:
            return nr, val
    if (srp.SRP_HEADER_SIZE + len(bytes(nr))) % 2:
        nr.dl.lst[1][2] = "+" + nr.dl.lst[1][2]
    return nr, val


def split_messages(data: bytes):
    """Cuts an SRP data region into GS messages by the 24-bit size at the start of each (size includes the header)."""
    out = []
    off = 0
    while off + gsm.GSMSG_HEADER_SIZE <= len(data):
        size = (data[off] << 16) + (data[off + 1] << 8) + data[off + 2]
        if size < gsm.GSMSG_HEADER_SIZE or off + size > len(data):
            break
        out.append(data[off:off + size])
        off += size
    return out


def handle_ping(data: bytes, addr: tuple[str, int], now: float, nat_port: int = 45000):
    pid = utils.read_u32_be(data)
    log(f"PING from {addr[0]}:{addr[1]} id={data.hex()}" + (f" on :{nat_port}" if nat_port != 45000 else ""))
    found = find_by_ping(pid)
    if found is None:
        log(f"  unknown ping id {data.hex()} (no matching NAT request)")
        nat_step_orphan_ping(addr, now)
        return []
    clt, r = found
    clt.last_seen = now
    if not r.get("extra") and nat_port == 45000:
        sx = SOCKET_EXT.setdefault(addr[0], {})
        if sx.get(str(r["sock"])) != addr[1]:
            log(f"  socket {r['sock']} of {addr[0]} shows external port {addr[1]}" + (f" (was {sx[str(r['sock'])]})" if str(r["sock"]) in sx else ""))
            sx[str(r["sock"])] = addr[1]
    if r.get("extra"):
        hit = r.get("hit", 0) + 1
        r["hit"] = hit
        if r.get("port"):
            HOST_TRAFFIC.setdefault(addr[0], {})[r["port"]] = now
            ext = HOST_EXT.setdefault(addr[0], {})
            locked = HOST_EXT_LOCK.setdefault(addr[0], set())
            # A ping only teaches the external port until the host's own game data has been seen on this hole: two
            # consoles behind one NAT both ping, and only the host sends game data (2026-11-05 00:16, port flip-flop).
            if r["port"] not in locked and ext.get(r["port"]) != addr[1]:
                log(f"  hole {r['port']}: {addr[0]} socket 1 shows external port {addr[1]}" + (f" (was {ext[r['port']]})" if r["port"] in ext else ""))
                ext[r["port"]] = addr[1]
        if hit <= 2 or hit % 50 == 0:
            log(f"  HOLE PING #{hit}: {addr[0]}:{addr[1]} reached port {r.get('port')}")
        return [(srp.ack_segment(clt), (clt.addr, clt.port))]
    if not r["pending"]:
        if NO_REPEAT_ACK:
            log(f"  repeat ping ignored (socket {r['sock']})")
            return []
        log(f"  -> bare ACK to {clt.addr}:{clt.port} (socket {r['sock']})")
        return [(srp.ack_segment(clt), (clt.addr, clt.port))]
    if NO_ADDRESS:
        log("  ping received, ADDRESS deliberately NOT sent (SCCT_NAT_NO_ADDRESS=1)")
        return []
    r["pending"] = False
    nat_step_ping(clt, r, now)
    if ADDRESS_DELAY_S > 0:
        log(f"  waiting {ADDRESS_DELAY_S*1000:.0f} ms before ADDRESS")
        time.sleep(ADDRESS_DELAY_S)
    # The game copies this address into the room info it creates, and other players dial it. For internet play with
    # the server on a home PC, a LAN client must be reported with the PUBLIC address (SCCT_NAT_PUBLIC_ADDR=1 and
    # SCCT_PUBLIC_IP set), otherwise outside players receive a 192.168.x.x host address. LAN-only play then needs
    # NAT loopback on the router, so leave this off for pure LAN sessions.
    rep_ip = addr[0]
    if PUBLIC_ADDR and PUBLIC_IP:
        try:
            import ipaddress as _ipa
            _a = _ipa.ip_address(rep_ip)
            if _a.is_private or _a.is_loopback or _a.is_link_local:
                rep_ip = PUBLIC_IP
        except ValueError:
            pass
    msg, _ = nat_reply(r["msg"], gsm.NAT_MSG.ADDRESS, utils.ipv4_to_u32_be(rep_ip), addr[1])
    out = bytes(srp.SRPResponse(None, clt, msg))
    log(f"  -> ADDRESS socket={r['sock']} {rep_ip}:{addr[1]} ({len(out)} bytes) to {clt.addr}:{clt.port}" + (f" (observed {addr[0]})" if rep_ip != addr[0] else ""))
    packets = [(out, (clt.addr, clt.port))]
    if str(r["sock"]) == "1" and not r.get("extra") and RELAY_HOLES:
        clt.hole_msg = r["msg"]
        clt.hole_target = None       # port socket 1 currently pings (None = still 45000)
        clt.hole_last = 0.0
        clt.hole_count = 0
        log(f"  multi-hole: socket 1 of {clt.addr} will be steered to {RELAY_HOLES[0]} now, other holes on demand")
    if NAT_EXTRA_PORTID and not RELAY_HOLES and str(r["sock"]) == "1" and not r.get("extra"):
        for xport in NAT_EXTRA_PORTID:
            try:
                xr, xpid = nat_reply(r["msg"], gsm.NAT_MSG.PORT_ID, 0, xport, pick_id=True)
                clt.reqs[xpid] = {"msg": r["msg"], "sock": r["sock"], "pending": True, "extra": True, "port": xport}
                xout = bytes(srp.SRPResponse(None, clt, xr))
                packets.append((xout, (clt.addr, clt.port)))
                log(f"  -> EXTRA PORT_ID socket=1 ping_id={xpid:#010x} port={xport} ({len(xout)} bytes) [experiment: does the game ping it?]")
            except Exception as e:
                log(f"  EXTRA PORT_ID failed: {e!r}")
    return packets


def handle_segment(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    packets = []
    clt = client.NatClient.find(addr, CLIENTS)
    req = srp.SRPRequest(data)
    hdr = req.segment.header
    flags = hdr.flags
    log(f"SRP {len(data)}B from {addr[0]}:{addr[1]} flags={[f for f in flags if f != 'SRP_ID']} seg={hdr.seg:#x} ack={hdr.ack:#x} data_size={hdr.data_size}")
    if clt is not None and srp.SRPHeaderFlags.SYN.name in flags:
        nat_step_waiting(clt, now, "restarted its NAT session (SYN)")
        CLIENTS.remove(clt)
        clt = None
    if clt is None and srp.SRPHeaderFlags.FIN.name not in flags:
        if len(CLIENTS) >= 4096:
            log("  NAT table full, ignoring")
            return packets
        clt = client.NatClient(addr, None, None)
        clt.reqs = {}
        CLIENTS.append(clt)
        log(f"  added client {clt.addr}:{clt.port}")
    if clt is not None:
        clt.last_seen = now
    if req.segment.window and clt is not None:
        out = bytes(srp.SRPResponse(req, clt))
        packets.append((out, addr))
        log(f"  -> SYN+ACK ({len(out)} bytes)")
    elif req.segment.msg and clt is not None:
        region = data[srp.SRP_HEADER_SIZE:srp.SRP_HEADER_SIZE + hdr.data_size]
        chunks = split_messages(region)
        log(f"  data region {len(region)}B holds {len(chunks)} message(s)")
        answered = 0
        for chunk in chunks:
            try:
                m = gsm.Message(b'', in_buf=chunk)
            except Exception as e:
                log(f"  cannot parse message ({len(chunk)}B): {e!r}")
                continue
            if m.header.type != gsm.MESSAGE_TYPE.NAT:
                log(f"  non-NAT message {m.header.type.name} ignored")
                continue
            sock = m.dl.lst[1][0]
            try:
                NAT_NAMES[clt.addr] = str(m.dl.lst[1][1])
            except (IndexError, TypeError, AttributeError):
                pass
            if PORTID_LIMIT and answered >= PORTID_LIMIT:
                log(f"  NAT request socket={sock}: NOT answered (SCCT_NAT_PORTID_LIMIT={PORTID_LIMIT})")
                continue
            answered += 1
            nr, pid = nat_reply(m, gsm.NAT_MSG.PORT_ID, 0, nat_port, pick_id=True)
            clt.reqs[pid] = {"msg": m, "sock": sock, "pending": True, "t": now}
            out = bytes(srp.SRPResponse(req, clt, nr))
            packets.append((out, addr))
            log(f"  NAT request socket={sock}: {m.dl}")
            log(f"  -> PORT_ID socket={sock} ping_id={pid:#010x} port={nat_port} ({len(out)} bytes)")
    elif clt is not None and srp.SRPHeaderFlags.FIN.name not in flags:
        log("  bare ACK from client (no reply needed)")
    if srp.SRPHeaderFlags.FIN.name in flags:
        if clt is not None and client.NatClient.find((clt.addr, clt.port), CLIENTS):
            nat_step_waiting(clt, now, "closed its NAT session (FIN)")
            CLIENTS.remove(clt)
            log(f"  removed client {clt.addr}:{clt.port}")
    return packets


# ─── UDP relay (SCCT_RELAY) ─────────────────────────────────────────────────────────────────────────────────────
# A console behind a port-restricted NAT / inbound-filtering ISP can only be reached from the exact server endpoint it
# keeps talking to: this service on 45000, which the game pings from its 3658 socket every ~25 s. The relay uses that
# hole. The lobby server (ubigs_router_wm_server.py, SCCT_RELAY=1) hands an outside joiner the relay's address instead
# of the host's and tells this process "joiner J -> host H" over the control port. Joiner traffic arriving on the relay
# ports (3658, 10070-10080) is forwarded to H:3658 FROM 45000; whatever H sends back to 45000 from its game socket is
# forwarded to J from the relay port J used. v1: one relayed joiner per host (the host sees every relayed peer as
# <relay>:45000, so a second joiner would collide).
RELAY_PORTS: list[int] = []
RELAY_CTRL_PORT = 0
RELAY_HOST_PORT = int(os.environ.get("SCCT_RELAY_HOST_PORT", "3658"))
# Source port of the game's SRP/NAT control session (never relayed).
RELAY_SRP_PORT = int(os.environ.get("SCCT_RELAY_SRP_PORT", "10070"))
# SCCT_RELAY_ALT_HOST_PORTS=1: a joiner's 2nd/3rd/4th connection (new source port) is delivered to the host's other
# presolved sockets (10071, 10072, 10073; all punched to 45000) instead of 3658, so the host sees distinct links and its
# replies (from that socket) can be routed back to the right joiner port. Natively the host tells links apart by the
# joiner's source port; through the relay they all arrive from <relay>:45000 and the host drops the 2nd connect.
RELAY_ALT_HOST_PORTS = [int(x) for x in os.environ.get("SCCT_RELAY_ALT_HOST_PORTS_LIST", "10071,10072,10073").split(",")] \
    if os.environ.get("SCCT_RELAY_ALT_HOST_PORTS", "0").strip() == "1" else []
# SCCT_NAT_EXTRA_PORTID=45001[,45002]: after socket 1's ADDRESS reply, send the console extra PORT_ID messages for socket 1
# pointing at these ports. If the game pings them, its 3658 socket has a second NAT hole (<relay>:45001 -> host:3658) that
# the relay could use as a distinct source for a second link. Experiment: only logs the pings (bare ACK, no ADDRESS).
NAT_EXTRA_PORTID = [int(x) for x in os.environ.get("SCCT_NAT_EXTRA_PORTID", "").replace(";", ",").split(",") if x.strip()]
# SCCT_RELAY_HOLES=45001,45002: multi-hole mode. Test of 2026-11-04 17:08 showed that an unsolicited PORT_ID makes the
# console's 3658 socket ping the NEW port instead of the old one (it replaces the target, it does not add one). So the
# service re-sends a PORT_ID for socket 1 every SCCT_RELAY_HOLE_REFRESH_S seconds, cycling through these ports: the console
# pings each in turn and keeps every hole open. The relay then uses one hole port per joiner link as its source, so the
# host sees distinct source ports per link (as it would natively) and its replies arrive on distinct relay sockets.
RELAY_HOLES = [int(x) for x in os.environ.get("SCCT_RELAY_HOLES", "").replace(";", ",").split(",") if x.strip()]
# Steering budget: the console's SRP receive window is 536 B and unsolicited PORT_IDs (~46 B) are apparently never freed
# from it; after ~11 of them the console stopped reacting (2026-11-04 17:19). So a steer is sent only when a hole that is
# NEEDED (the first hole, or one a live joiner link uses) has seen no host traffic for SCCT_RELAY_HOLE_STALE_S seconds,
# at most one every SCCT_RELAY_HOLE_MIN_S seconds per console. Typical session: login, game link, match start, match end.
# A consumer (Linux conntrack) router keeps an established UDP 5-tuple ~180 s; refresh a needed hole after this much idle.
HOLE_STALE_S = float(os.environ.get("SCCT_RELAY_HOLE_STALE_S", "100"))
# Hard cap on steering messages per login. The console acks each PORT_ID over SRP and frees the window, so a SERIALIZED
# steer (one per confirmed ping / HOLE_MIN_S apart, as hole_steering does) can go well past the old limit of 10: on
# 2026-11-05 19:xx the console acked 18 segments with no stall and only our self-imposed cap of 10 dropped DORDOR's 11th
# link. A joiner behind a symmetric NAT (Windows ICS) gets a fresh external port per match, so a long 3-player session
# needs dozens. The "stops reacting around 11" seen on 2026-11-04 was unserialized steers overflowing the 536 B window.
# Correction, logs of 2026-11-06: serialized steers stop working too. Steers 1-11 got a first ping, 12-14 got none, so the
# console itself stops at about 11 per login (one PS2 host; not checked on other consoles or PCSX2). See HOLE_ADAPTIVE.
HOLE_BUDGET = int(os.environ.get("SCCT_RELAY_HOLE_BUDGET", "40"))
# Joiner source port -> hole index (the console uses sockets 10071-10073 for its outbound links and reuses each link
# later - a game link went idle for minutes and came back for a rematch - so the mapping must be permanent).
HOLE_PORT_BASE = int(os.environ.get("SCCT_RELAY_HOLE_PORT_BASE", "10071"))
# A steer does not make the console ping at once (it only re-targets its 25 s cycle), so a steered hole counts as fresh
# for HOLE_MIN_S and steers are at least HOLE_MIN_S apart per console.
HOLE_MIN_S = float(os.environ.get("SCCT_RELAY_HOLE_MIN_S", "30"))
HOLE_SOCKS: dict = {}       # port -> socket (bound hole ports)
HOST_TRAFFIC: dict = {}     # host ip -> {hole port: monotonic time of the last datagram from that host's 3658 on it}
# host ip -> {hole port: external port the host's 3658 socket shows on that hole}. A router may give a re-created
# mapping a different external port (2026-11-04 18:26: 3658 became 1096 on two holes while the third kept 3658).
HOST_EXT: dict = {}
HOST_EXT_LOCK: dict = {}   # host ip -> {hole ports whose external port was confirmed by the host's game data}
# host ip -> {socket no ("1".."4"): external port seen on that socket's own NAT pings to 45000}. Used by the alt-socket
# mode: every console socket keeps its 45000 hole open by itself (ping every 25 s), so a link delivered to socket k
# from 45000 needs no steering at all.
SOCKET_EXT: dict = {}
# Hole keepalive: a conntrack entry that has carried traffic both ways is refreshed by ANY packet through it, so the relay
# keeps such a hole open itself by sending a datagram inbound through it when it has been quiet for this long.
HOLE_KEEPALIVE_S = float(os.environ.get("SCCT_RELAY_HOLE_KEEPALIVE_S", "45"))
HOLE_KEEPALIVE_PAYLOAD = bytes.fromhex(os.environ.get("SCCT_RELAY_HOLE_KEEPALIVE_HEX", "0000"))  # 2 B (some routers drop empty UDP)
# Dead-hole rule: a joiner link is sending through a hole but the host has been silent on it for this long -> steer.
HOLE_DEAD_S = float(os.environ.get("SCCT_RELAY_HOLE_DEAD_S", "15"))
# A hole the host STOPPED pinging is still open on the host's NAT until the mapping expires (~60 s of no host traffic).
# So a new joiner link can REUSE a hole the host sent through up to this long ago with no steer: the first reply the host
# sends refreshes it. Wider than HOLE_DEAD_S (which is only about when to give up and steer) - this is what lets 3 joiners
# whose NATs churn ports every match keep going without a steer per joiner per match (2026-11-05 20:40 4-player session).
HOLE_REUSE_S = float(os.environ.get("SCCT_RELAY_HOLE_REUSE_S", "50"))
# Pre-open this many holes per console right after login (one per ping cycle, each confirmed by its ping before the
# next steer) and keep them warm with keepalives, so joiners' room and game links find open holes immediately.
HOLE_PREOPEN = int(os.environ.get("SCCT_RELAY_HOLE_PREOPEN", "1"))   # (informational; one spare hole is kept ready)
RELAY_TRAFFIC: dict = {}    # host ip -> {hole port: time of the last datagram the relay sent through it (joiner data or keepalive)}
KA_STATE: dict = {}         # keepalive counters for consoles that have no joiner flow yet (pre-opened holes)
LINK_SUMMARY_S = float(os.environ.get("SCCT_RELAY_LINK_SUMMARY_S", "30"))   # per-link activity summary interval (0 = off)
LAST_SUMMARY = 0.0
LINK_IDLE_S = 5.0           # a joiner endpoint silent for longer is not "live"
# Owner keep (SCCT_RELAY_OWNER_KEEP, default on). 5 s of silence is shorter than the game's normal pauses: every link
# goes quiet ~7-9 s while a map loads, and a room link is quiet for a whole match and comes back at the revenge swap.
# 2026-11-15 Cinema (COOLDOR host): at 05:36:36 LINEBACKER's game link was given DORIAN_D's quiet room-link hole; at the
# revenge swap DORIAN_D's room link got another hole, and at 05:38:21, 7 s into his load pause, LINEBACKER's room link
# was moved onto it (and ANDROID-DOR's onto DORIAN_D's game-link hole). DORIAN_D came back 2 s later on a hole the host
# had not opened and his game left the room. Now another player's link keeps its hole while it has been quiet at most
# OWNER_PAUSE_S. (A first version also kept a player's oldest link's hole for 300 s during a match as its "room link":
# 2026-11-15 06:40 that wrongly protected DORIAN_D's finished game-link hole 45001 - the PS2 rotates its three sockets,
# none is a fixed room link - and blocked the only proven hole ANDROID-DOR's rescue could have used. On a strict host a
# hole quiet for a whole match is gone anyway; see HOLD_COLD_STRICT_S.) Only the player's own links, a player who
# left, or the last-resort takeover can take it.
OWNER_KEEP = os.environ.get("SCCT_RELAY_OWNER_KEEP", "1").strip().lower() not in ("0", "off", "no", "false", "")
OWNER_PAUSE_S = float(os.environ.get("SCCT_RELAY_OWNER_PAUSE_S", "15"))
# Own-socket keep (SCCT_RELAY_OWN_SOCKET_KEEP, default on). The host's game knows each player socket by the relay hole its
# packets come from, so two of one player's sockets must never take turns on one hole. 2026-11-15 17:25:49, Krauser map
# start (COOLDOR host, no forward): DORIAN_D's socket 10073 came back on a dead hole and was given the "ready spare"
# 45009 - the hole his socket 10071 had used all through the match before, paused 20 s for the load. Match 5 played on it,
# but at the 17:29:46 revenge swap 10071 came back, found 45009 taken, got 45004; 3 s later the host stopped sending to
# 45009 for good, 10073 starved and DORIAN_D's PS2 left the room 7 s after the match (back to the online lobby). The one
# such hand-off of the day, and the one boot. Now a socket the relay already knows (it has a hole, or the relay saw it
# before - with 10-minute halves a socket often loses its idle hole while away) does not take the hole of
# another of the same player's sockets that has shown it comes back - it returned at least once after OWN_RETURN_S of
# silence (DORIAN_D's 10071 had, at 17:12 and 17:20) - while that socket is known to the relay, not leaving and quiet at
# most OWN_SOCKET_KEEP_S. A socket that never came back (a game link on a fresh port, a phone's ports) is not protected:
# keeping its hole would only make the room link wait for a ping. New ports are unchanged: a join probe and its room link
# still share, a VPN port change still follows its paused link (REBIND), and a phone's fresh ports (ANDROID-DOR) still
# reuse that player's old holes.
OWN_SOCKET_KEEP = os.environ.get("SCCT_RELAY_OWN_SOCKET_KEEP", "1").strip().lower() not in ("0", "off", "no", "false", "")
OWN_SOCKET_KEEP_S = float(os.environ.get("SCCT_RELAY_OWN_SOCKET_KEEP_S", "1800"))   # a whole map (two halves)
OWN_RETURN_S = float(os.environ.get("SCCT_RELAY_OWN_RETURN_S", "60"))


def holds_hole(hv: dict, e, ep, now: float) -> bool:
    """Endpoint e keeps its hole against a link of endpoint ep (see OWNER_KEEP and OWN_SOCKET_KEEP)."""
    if e not in hv.get("eps", {}):
        return False
    quiet = now - hv["eps"][e][1]
    if quiet <= LINK_IDLE_S:
        return True
    if e in hv.get("leaving", {}):
        return False
    if e[0] == ep[0]:
        # the same player's other socket: only against a socket that already has a hole of its own (see OWN_SOCKET_KEEP)
        return (OWN_SOCKET_KEEP and (ep in hv.get("src", {}) or ep in hv.get("known", set()))
                and e not in hv.get("probe", set()) and e in hv.get("returned", set()) and quiet <= OWN_SOCKET_KEEP_S)
    if not OWNER_KEEP:
        return False
    return quiet <= OWNER_PAUSE_S


LINK_ESTABLISHED = 20       # (no longer used by pick_slot: every joiner endpoint keeps a dedicated hole)
RELAY_IDLE_S = float(os.environ.get("SCCT_RELAY_IDLE_S", "300"))   # flow dropped after this long without traffic (mappings no longer follow lobby leaves)
RELAY_MAP: dict = {}        # joiner_ip -> {"host": host_ip, "room": ..., "user": ..., "ts": t}
RELAY_HOSTS: dict = {}      # host_ip -> {"joiner": (ip, port), "sock": relay socket, "rx": n, "tx": n, "last": t, "first": t}
RELAY_UNMAPPED_LOGGED: set = set()
RELAY_SNAPSHOT = os.environ.get("SCCT_RELAY_SNAPSHOT", "state/relay_map.json")
# SCCT_RELAY_DUMP=N: hex-dump the first N datagrams of each direction per host flow (to learn the peer protocol).
RELAY_DUMP = int(os.environ.get("SCCT_RELAY_DUMP", "0") or 0)
# SCCT_RELAY_FANOUT_S: the host's replies go to the LAST joiner endpoint that sent; with a value > 0 they also go to
# every other endpoint of the same joiner IP seen within that many seconds (a client may use two ports for one join).
RELAY_FANOUT_S = float(os.environ.get("SCCT_RELAY_FANOUT_S", "0") or 0)
NAT_MAIN_SOCK = None        # the 45000 socket (host-facing side of the relay)
IPCHG_LOG_S = 60.0          # "joiner IP changes" is logged at most this often per (host, new joiner IP)
_IPCHG_LOGGED: dict = {}
# Adaptive steering. Logs of 2026-11-06/07: a PS2 stopped honouring steers after 11 in one login (12-14 got no ping although
# HOLE_BUDGET is 40), but the same host answered on holes it had not pinged for nearly an hour, 6 times out of 6, 10-14 s after
# the link opened. So its NAT lets a joiner's packets in without a ping first and most steers are wasted on it.
# Evidence is always collected and logged. SCCT_RELAY_ADAPTIVE_STEER=1 also acts on it. Off by default: one host's data so far.
# Steers per login the game is known to honour (log evidence only: 11 on PS2 and PCSX2 hosts). Only used for the warning below.
HOLE_GAME_LIMIT = int(os.environ.get("SCCT_RELAY_STEER_GAME_LIMIT", "11") or "11")
HOLE_ADAPTIVE = os.environ.get("SCCT_RELAY_ADAPTIVE_STEER", "0").strip().lower() not in ("0", "off", "false", "no", "")
HOLE_OPEN_GAP_S = float(os.environ.get("SCCT_RELAY_HOLE_OPEN_GAP_S", "200"))    # longer than any mapping lasts without host traffic
HOLE_OPEN_HITS = int(os.environ.get("SCCT_RELAY_HOLE_OPEN_HITS", "2"))          # evidence needed before a host counts as open
HOLE_OPEN_MISSES = int(os.environ.get("SCCT_RELAY_HOLE_OPEN_MISSES", "2"))      # rescues in a row that turn it back off
HOLE_OPEN_WAIT_S = float(os.environ.get("SCCT_RELAY_HOLE_OPEN_WAIT_S", "20"))   # how long an open host may stay silent before a rescue steer
HOLE_ADAPT_GAP_S = float(os.environ.get("SCCT_RELAY_HOLE_ADAPT_GAP_S", "26"))   # steers at least one ping cycle (25 s) apart
HOLE_ADAPT_SPARE_MAX = int(os.environ.get("SCCT_RELAY_HOLE_ADAPT_SPARE_MAX", "8"))  # spare steers stop here; the rest of the ~11 are kept for rescues
HOST_NAT: dict = {}         # host ip -> {"hits": n, "misses": n, "open": bool}; survives re-logins, cleared by a server restart
# Warm holes (SCCT_RELAY_WARM_HOLES=1). Every map load (match start, revenge round, back to the lobby) makes each joiner's
# game RECONNECT to the host, and it rotates its three game sockets (10071-10073) between loads, so the hole a joiner
# comes back on has usually been quiet for a round or two. The host's NAT (or PCSX2 Sockets mode, ~120 s) has often
# forgotten it by then and the reconnect dies (Mount Hospital revenge, 2026-11-12). The console pings only one port, once
# every 25 s (clGameSocket::Send_Ping: > 24 s since the last ping; SetPingPort keeps that timer), so the relay steers that
# ping round the holes of each joiner's least recently used socket, coldest first. With fewer than WARM_FULL joiners every
# other steer goes back to a spare hole, so a new joiner still finds a ready one. Needs the SRP segment wrap fix.
WARM_HOLES = os.environ.get("SCCT_RELAY_WARM_HOLES", "0").strip().lower() in ("1", "on", "true", "yes")
WARM_GAP_S = float(os.environ.get("SCCT_RELAY_WARM_GAP_S", "26"))      # one console ping cycle between warm steers
WARM_AGE_S = float(os.environ.get("SCCT_RELAY_WARM_AGE_S", "40"))      # a hole the host sent through more recently is warm
WARM_MAX_S = float(os.environ.get("SCCT_RELAY_WARM_MAX_S", "900"))     # endpoints unused for longer are not kept warm
WARM_FULL = int(os.environ.get("SCCT_RELAY_WARM_FULL", "3"))           # joiners in a full 2v2 room: no spare needed then
# Wake move (SCCT_RELAY_WAKE_MOVE=1). A joiner socket that comes back after a long silence is the game RECONNECTING after a
# map load (a new connection, not a continued one), so it may as well arrive on a different hole. If its own hole has gone
# cold (host silent on it) the relay gives it a hole the host has sent through recently and that no other link is using:
# usually the hole that joiner's previous-round socket just stopped using, or the spare. Replay of the 2026-11-07..12 logs:
# for a host that drops quiet holes after ~2 min, reconnects landing on a dead hole fall from 72 to 17 of 78 (with warm holes).
WAKE_MOVE = os.environ.get("SCCT_RELAY_WAKE_MOVE", "0").strip().lower() in ("1", "on", "true", "yes")
WAKE_IDLE_S = float(os.environ.get("SCCT_RELAY_WAKE_IDLE_S", "60"))    # silence that makes a socket's next packet a reconnect
WAKE_COLD_S = float(os.environ.get("SCCT_RELAY_WAKE_COLD_S", "40"))    # own hole counts as cold after this much host silence
WAKE_WARM_S = float(os.environ.get("SCCT_RELAY_WAKE_WARM_S", "30"))    # a hole the host sent through this recently is warm
# Silence log (log only). For every relayed player link: a line when the PLAYER stops sending while the host still sends
# to it ("went silent"), when the HOST stops answering a player who is still sending ("host went silent"), and when either
# side comes back. GROUP_LEAVE also logs each link's last activity before its counters are reset. 2026-11-11: Snake left
# Station mid-match and the 30 s summaries could not show whether his game froze first. SCCT_RELAY_SILENCE_LOG=0: off.
SILENCE_LOG = os.environ.get("SCCT_RELAY_SILENCE_LOG", "1").strip().lower() not in ("0", "off", "false", "no")
SILENCE_S = float(os.environ.get("SCCT_RELAY_SILENCE_S", "3"))
SILENCE_MIN_RX = int(os.environ.get("SCCT_RELAY_SILENCE_MIN_RX", "20"))   # links with fewer packets (lobby probes) are skipped
# AGE log (SCCT_RELAY_AGE_LOG, default on; log only - no packet is handled differently). One "AGE" line per link that starts,
# or comes back after more than LINK_IDLE_S, on a relay hole: how long the host had been silent on that hole (game data, and
# any packet including pings), whether the relay judged it cold, every hold / move / ready spare on the way, and how long
# until the host's first packet reached the player - or outcome=never after AGE_TIMEOUT_S. 2026-11-15: the Nov 13-15 logs
# could not say how long COOLDOR's router keeps a hole (no first-reply time per link; the Factory room link coming back on
# hole 45007 was not even logged), so every cold-age value so far was a guess. These lines are the measurement.
AGE_LOG = os.environ.get("SCCT_RELAY_AGE_LOG", "1").strip().lower() not in ("0", "off", "false", "no")
AGE_TIMEOUT_S = float(os.environ.get("SCCT_RELAY_AGE_TIMEOUT_S", "70"))
# Slow join lines (SCCT_RELAY_SLOW_JOIN_LOG, default off; LOG ONLY, needs AGE LOG). 2026-11-28: the game's settings
# (ON_LINS.UMD, ShadowStrikePS2.ini [IpDrv.TcpNetDriver] InitialConnectTimeout=60.0) give a new connection 60 s to come
# up, then the joining console gives up. A 'SLOW JOIN' line is written once when a new link (kind=new, not a link coming
# back after a pause) has had no host packet for SLOW_JOIN_WARN_S, and its AGE line says so when the host's first packet
# came after 60 s. A link that comes back after a pause is an existing connection (ConnectionTimeout=500 s): not counted.
SLOW_JOIN_LOG = os.environ.get("SCCT_RELAY_SLOW_JOIN_LOG", "0").strip().lower() not in ("0", "off", "false", "no", "")
SLOW_JOIN_WARN_S = float(os.environ.get("SCCT_RELAY_SLOW_JOIN_WARN_S", "50"))
GAME_JOIN_LIMIT_S = 60.0
AGE_PENDING: dict = {}      # (host ip, joiner endpoint) -> attempt record, until the host's first packet for it
NAT_NAMES: dict = {}        # console ip -> player name from its NAT requests (names hosts in AGE lines)
# Shadow hole classes (SCCT_RELAY_SHADOW, default on): LOG ONLY, no routing change. 2026-11-16: COOLDOR with UPnP on was
# judged strict twice although fresh holes reached his PC (46001 test packet; ANDROID-DOR's join on never-pinged hole
# 45004 at 05:36:59), because one previously used hole (45001, idle ~45-100 s) dropped 17 s of DORIAN_D's packets. One
# open/strict verdict per host cannot describe that router. These lines keep evidence per kind of hole - pinged (the host
# sent there <= HOLD_PING_OPEN_S ago), fresh (never, or >= SHADOW_FRESH_S ago), stale (in between) - and say, for every
# link that starts or returns on a fresh or stale hole, what a fresh-hole rule WOULD do. Stage 1: read the logs before
# any of it is allowed to route.
SHADOW = os.environ.get("SCCT_RELAY_SHADOW", "1").strip().lower() not in ("0", "off", "false", "no")
SHADOW_FRESH_S = float(os.environ.get("SCCT_RELAY_SHADOW_FRESH_S", "300"))
SHADOW_TEST_S = float(os.environ.get("SCCT_RELAY_SHADOW_TEST_S", "4"))
SHADOW_TEST_PKTS = int(os.environ.get("SCCT_RELAY_SHADOW_TEST_PKTS", "5"))
SHADOW_PROVEN = int(os.environ.get("SCCT_RELAY_SHADOW_PROVEN", "2"))
SHADOW_SUMMARY_S = float(os.environ.get("SCCT_RELAY_SHADOW_SUMMARY_S", "600"))
SHADOW_PROBE: dict = {}     # (host, hole) -> test record while a joiner sends into a fresh/stale hole
SHADOW_DONE: dict = {}      # (host, hole) -> time its last test ended (no new test on that hole for 60 s)
SHADOW_EV: dict = {}        # host -> {"fresh": [ok, no-reply, ambiguous], "stale": [...], "would": n, "links": n}
SHADOW_HINT: dict = {}      # public ip -> time of its last scopsDNS "upnp_open" hint
SHADOW_SUM_T: dict = {}     # host -> time of its last SHADOW summary line
# scopsDNS 2.1 sends {"op":"upnp_open"} to 45000 from the host's PC. Before this, a datagram from a relayed host's IP that
# is not a 4-byte ping was relayed to its joiner as game data - so on the host's own network the hint went into the joiner's
# game every 20 minutes. It is now caught, logged, and never relayed (SCCT_RELAY_UPNP_HINT=0: old behaviour).
UPNP_HINT = os.environ.get("SCCT_RELAY_UPNP_HINT", "1").strip().lower() not in ("0", "off", "false", "no")
# UPnP router probe (SCCT_RELAY_UPNP_PROBE, default on; LOG ONLY). 2026-11-16 17:53: with COOLDOR (UPnP green) the shadow
# tests could not prove fresh holes - players land on new holes almost only at map starts, and the host's answer then
# falls in its first burst after the load (ambiguous). The SCOPS window (V7) now also forwards UDP 3659 to the PC and asks
# for this test: the server sends small packets to <public ip>:3659 from ports that PC has never talked to (fresh) and,
# 45 s after the PC answered on one of them, from that port again (stale, the 45001 case). The window reports what
# arrived. That is the same router and the same forward mechanism as the PS2's, without the game or a map load in the way.
UPNP_PROBE = os.environ.get("SCCT_RELAY_UPNP_PROBE", "1").strip().lower() not in ("0", "off", "false", "no")
_pp = os.environ.get("SCCT_RELAY_UPNP_PROBE_PORTS", "46001-46020").split("-")
PROBE_PORTS = list(range(int(_pp[0]), int(_pp[-1]) + 1))   # server ports the probe sends from (outbound only)
PROBE_STALE_S = float(os.environ.get("SCCT_RELAY_UPNP_PROBE_STALE_S", "45"))
PROBE_WAIT_S = 5.0          # a probe not reported within this is "not seen"
PROBE_EVERY_S = 600.0       # one probe run per public address per 10 minutes
PROBE_RUNS: dict = {}       # ip -> run in progress
PROBE_LAST: dict = {}       # ip -> time its last run ended
PROBE_PORT_USE: dict = {}   # (ip, server port) -> last time that port sent to that ip
# Failure-step lines (SCCT_RELAY_FAIL_LOG, default on; LOG ONLY, nothing here changes a routing decision). Asked for on
# 2026-11-16 after COOLDOR's UPnP evening: say WHERE a console stopped, not only that it did.
#  - NAT step: a console socket that got its PORT_ID but never pinged 45000 (its NAT test stopped there), a late ping, and
#    a NAT session closed while a socket was still waiting. These are the console's login steps (one set per login), not
#    per join: a join is game traffic between consoles that the relay only forwards, and its outcome is the AGE line.
#  - FAIL why: on a no-reply move, when the host last used the dead hole before the link started, which other holes it was
#    answering, and the router probe's last result, so "router dropped a hole it had just used" (COOLDOR 45002, 18:12)
#    reads apart from "the hole was cold".
#  - AGE lines name the holes the relay was avoiding for that host (marked bad, 600 s) when the link started.
#  - The "looks address-restricted" line lists the evidence that made it.
FAIL_LOG = os.environ.get("SCCT_RELAY_FAIL_LOG", "1").strip().lower() not in ("0", "off", "false", "no", "")
NAT_STALL_S = float(os.environ.get("SCCT_RELAY_NAT_STALL_S", "10"))   # PORT_ID sent, no ping this long = stalled
def _env_on(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "off", "false", "no", "")
# ---- Relay guard (SCCT_RELAY_GUARD_LOG, default OFF; LOG ONLY: nothing is ever dropped or changed) ----
# Data for a possible later flood limit (an idea from tubisoft's 187 relay: a per-player packet-rate cap and a packet size
# cap). Before any limit could be safe we need the real peaks, above all at map loads, revenge swaps and back to the
# lobby, where the game sends its bursts. For every relayed link (joiner -> host and host -> joiner, per hole) it keeps the
# busiest 1-second window (packets and bytes) and the largest packet, and logs:
#   GUARD would-limit: <link> sent N pkt in 1 s (limit L)       first time a link passes SCCT_RELAY_GUARD_PPS in a window
#   GUARD would-limit: <link> packet of N B (limit L)            first packet larger than SCCT_RELAY_GUARD_MAX_PKT
#   GUARD peaks (last 10 min): ...                               every SCCT_RELAY_GUARD_SUMMARY_S, busiest links first
#   GUARD unmapped senders (last 10 min): ...                    packets to relay ports from IPs the lobby never mapped
# ("Only relay for signed-in players" is already how the relay works: a joiner IP is only forwarded after the lobby server
# maps it when that player joins a room, and unmapped IPs are dropped. The unmapped-sender count shows whether anyone is
# poking the relay ports.) Repeated would-limit lines for one link are limited to one a minute.
GUARD_LOG = _env_on("SCCT_RELAY_GUARD_LOG", "0")
GUARD_PPS = int(os.environ.get("SCCT_RELAY_GUARD_PPS", "300"))
GUARD_MAX_PKT = int(os.environ.get("SCCT_RELAY_GUARD_MAX_PKT", "1472"))
GUARD_SUMMARY_S = float(os.environ.get("SCCT_RELAY_GUARD_SUMMARY_S", "600"))
GUARD_LINKS = {}       # key -> {"who", "sec", "n", "b", "pk_n", "pk_b", "pk_at", "big", "tot", "over", "warn_t"}
GUARD_UNMAPPED = {}    # ip -> [packets, bytes]
_GUARD_LAST = [0.0]
_GUARD_ERR = []


def guard_count(key, who: str, size: int, now: float) -> None:
    """Count one forwarded packet for a link. Log only; never raises."""
    if not GUARD_LOG:
        return
    try:
        g = GUARD_LINKS.get(key)
        if g is None:
            g = GUARD_LINKS[key] = {"who": who, "sec": -1, "n": 0, "b": 0, "pk_n": 0, "pk_b": 0, "pk_at": "",
                                    "big": 0, "tot": 0, "over": 0, "warn_t": -1e9}
        sec = int(now)
        if sec != g["sec"]:
            g["sec"], g["n"], g["b"] = sec, 0, 0
        g["n"] += 1
        g["b"] += size
        g["tot"] += 1
        if g["n"] > g["pk_n"]:
            g["pk_n"], g["pk_at"] = g["n"], now_ts()
        g["pk_b"] = max(g["pk_b"], g["b"])
        over = None
        if g["n"] == GUARD_PPS + 1:
            over = f"sent {g['n']} pkt in 1 s (limit {GUARD_PPS})"
        if size > GUARD_MAX_PKT and size > g["big"]:
            over = f"packet of {size} B (limit {GUARD_MAX_PKT})"
        g["big"] = max(g["big"], size)
        if over:
            g["over"] += 1
            if now - g["warn_t"] >= 60:
                g["warn_t"] = now
                log(f"GUARD would-limit: {who} {over} (log only, nothing dropped)")
    except Exception as e:  # logging must never affect forwarding
        if not _GUARD_ERR:
            _GUARD_ERR.append(e)
            log(f"GUARD error (logging off from here): {e!r}")


def guard_unmapped(ip: str, size: int) -> None:
    if not GUARD_LOG:
        return
    try:
        u = GUARD_UNMAPPED.setdefault(ip, [0, 0])
        u[0] += 1
        u[1] += size
    except Exception:
        pass


def guard_tick(now: float) -> None:
    """Every GUARD_SUMMARY_S: the busiest links of the period, then reset the period's peaks."""
    if not GUARD_LOG or _GUARD_ERR:
        return
    if _GUARD_LAST[0] == 0.0:
        _GUARD_LAST[0] = now
        return
    if now - _GUARD_LAST[0] < GUARD_SUMMARY_S:
        return
    try:
        mins = (now - _GUARD_LAST[0]) / 60.0
        _GUARD_LAST[0] = now
        busy = sorted(GUARD_LINKS.values(), key=lambda g: -g["pk_n"])
        if busy:
            top = "; ".join(f"{g['who']}: peak {g['pk_n']} pkt/s ({g['pk_b'] / 1024:.1f} KB/s) at {g['pk_at']}, "
                            f"largest {g['big']} B, {g['tot']} pkt" + (f", would-limit x{g['over']}" if g["over"] else "")
                            for g in busy[:6])
            log(f"GUARD peaks (last {mins:.0f} min, {len(busy)} links, limits {GUARD_PPS} pkt/s and {GUARD_MAX_PKT} B, "
                f"log only): {top}")
        if GUARD_UNMAPPED:
            worst = sorted(GUARD_UNMAPPED.items(), key=lambda kv: -kv[1][0])
            log(f"GUARD unmapped senders (last {mins:.0f} min): {len(worst)} IPs, {sum(v[0] for _, v in worst)} packets "
                f"dropped as before; top: " + ", ".join(f"{ip} {v[0]} pkt" for ip, v in worst[:5]))
        GUARD_LINKS.clear()
        GUARD_UNMAPPED.clear()
    except Exception as e:
        _GUARD_ERR.append(e)
        log(f"GUARD error (logging off from here): {e!r}")


# ---- Fair-handout shadow (SCCT_RELAY_FAIR_SHADOW, default OFF; LOG ONLY: no decision changes) ----
# 2026-11-19 (STRIDERSTROKE strict host, three joiners): at 00:36:02 the hole the relay had been keeping warm for DORIAN_D's
# lobby socket (45001) was handed to SNAKE's link, which arrived 0.1 s earlier; DORIAN_D then waited for a new steer. A
# "fair rule" would give a warmed hole only to the player it was warmed for. Before building that, these lines count how
# often it happens and what it costs:
#   FAIR shadow: hole H kept warm for <owner> (warm steer Ns ago) went to <taker>; fair rule would give <taker> <alt>
#   FAIR shadow: <owner> back Ns after its warm hole H went to <taker>: now on hole X (<why>)     (its AGE line has the wait)
#   FAIR summary (last 10 min): warm holes taken by another player N, owners back M (held/slow K), ...
FAIR_SHADOW = _env_on("SCCT_RELAY_FAIR_SHADOW", "0")
FAIR_WARM_S = float(os.environ.get("SCCT_RELAY_FAIR_WARM_S", "120"))      # a warm steer this recent marks the hole as kept
FAIR_BACK_S = float(os.environ.get("SCCT_RELAY_FAIR_BACK_S", "120"))      # owner back within this long counts as affected
FAIR_SUMMARY_S = float(os.environ.get("SCCT_RELAY_FAIR_SUMMARY_S", "600"))
FAIR_WARM_FOR = {}     # (host, hole) -> (endpoint, t of the warm steer)
FAIR_TAKEN = {}        # (host, owner endpoint) -> (t, hole, taker endpoint)
FAIR_STATS = {"taken": 0, "back": 0, "back_slow": 0, "alt": 0}
_FAIR_LAST = [0.0]
_FAIR_ERR = []


def _fair_safe(fn):
    def run(*a, **k):
        if not FAIR_SHADOW or _FAIR_ERR:
            return None
        try:
            return fn(*a, **k)
        except Exception as e:  # logging must never affect routing
            _FAIR_ERR.append(e)
            log(f"FAIR shadow error (logging off from here): {e!r}")
            return None
    return run


@_fair_safe
def fair_warm_steer(host: str, port: int, ep, now: float) -> None:
    FAIR_WARM_FOR[(host, port)] = (ep, now)


@_fair_safe
def fair_reuse(host: str, port: int, ep, popped: list, alts: list, now: float) -> None:
    """A new link took an open hole and dropped its idle occupants. If one of them is another player's endpoint the relay
    was keeping this hole warm for, note what a fair rule would have done instead."""
    w = FAIR_WARM_FOR.get((host, port))
    if w is None or now - w[1] > FAIR_WARM_S:
        return
    owner = w[0]
    if owner not in popped or owner[0] == ep[0]:
        return
    alt = next((pt for pt in alts if pt != port and (FAIR_WARM_FOR.get((host, pt)) is None
                                                      or now - FAIR_WARM_FOR[(host, pt)][1] > FAIR_WARM_S
                                                      or FAIR_WARM_FOR[(host, pt)][0][0] == ep[0])), None)
    FAIR_TAKEN[(host, owner)] = (now, port, ep)
    FAIR_STATS["taken"] += 1
    if alt is not None:
        FAIR_STATS["alt"] += 1
    log(f"FAIR shadow: hole {port} kept warm for {_who(owner[0])}:{owner[1]} (warm steer {now - w[1]:.0f}s ago) went to "
        f"{_who(ep[0])}:{ep[1]}; fair rule would give {_who(ep[0])} "
        + (f"open hole {alt} instead" if alt is not None else "a steered/spare hole instead (no other open hole)")
        + " (log only)")


@_fair_safe
def fair_back(host: str, ep, port: int, why: str, now: float) -> None:
    t = FAIR_TAKEN.pop((host, ep), None)
    if t is None or now - t[0] > FAIR_BACK_S:
        return
    slow = not why.startswith(("reuses open hole", "ready", "shares the hole"))
    FAIR_STATS["back"] += 1
    if slow:
        FAIR_STATS["back_slow"] += 1
    log(f"FAIR shadow: {_who(ep[0])}:{ep[1]} back {now - t[0]:.1f}s after its warm hole {t[1]} went to "
        f"{_who(t[2][0])}:{t[2][1]}: now on hole {port} ({why})" + (" - likely a slow return" if slow else "") + " (log only)")


@_fair_safe
def fair_tick(now: float) -> None:
    if _FAIR_LAST[0] == 0.0:
        _FAIR_LAST[0] = now
        return
    if now - _FAIR_LAST[0] < FAIR_SUMMARY_S:
        return
    mins = (now - _FAIR_LAST[0]) / 60.0
    _FAIR_LAST[0] = now
    st = FAIR_STATS
    if st["taken"] or st["back"]:
        log(f"FAIR summary (last {mins:.0f} min, log only): warm holes taken by another player {st['taken']} "
            f"(another open hole was free for the taker {st['alt']}), owners back within {FAIR_BACK_S:.0f}s {st['back']}, "
            f"of them on a hole that needed a steer/hold (likely slow) {st['back_slow']}")
    for k in st:
        st[k] = 0
    for k, v in list(FAIR_TAKEN.items()):
        if now - v[0] > FAIR_BACK_S:
            FAIR_TAKEN.pop(k, None)


# Leave grace (SCCT_RELAY_LEAVE_GRACE, default on). GROUP_LEAVE used to forget all of a player's links at once. A link the
# PS2 was still sending on (its goodbye, or heartbeats for a second or two) was then treated as a NEW link and put on
# another hole, so the host saw the same player from a second address mid-conversation. 2026-11-13 05:29:16: that froze
# Dorian_D's PS2; at 05:28:25 the goodbye packets left a stale mapping that split the next join across two holes (the join
# never completed). Now a link still sending at the leave keeps its hole until it has been quiet for LEAVE_IDLE_S.
LEAVE_GRACE = _env_on("SCCT_RELAY_LEAVE_GRACE", "1")
LEAVE_LIVE_S = float(os.environ.get("SCCT_RELAY_LEAVE_LIVE_S", "1.5"))   # sent this recently at the leave = still live
LEAVE_IDLE_S = float(os.environ.get("SCCT_RELAY_LEAVE_IDLE_S", "3"))     # forgotten once quiet this long
# Join clean slate (on with LEAVE_GRACE). A JOIN_ROOM forgets every link of that player that is not sending right now, so
# the join's first contact and its game link are both new and share one hole (every join that worked did so; the one that
# split them failed, 2026-11-13 05:28:47).
JOIN_IDLE_S = float(os.environ.get("SCCT_RELAY_JOIN_IDLE_S", "1.5"))
# SRP session by address (SCCT_RELAY_SRP_BY_SESSION, default on). A relayed host's packets from any port other than
# RELAY_SRP_PORT (10070) were taken as game data, and a NAT-client entry on that port was dropped as "stray". A host
# behind a router that rewrites source ports has its SRP/NAT session on another port (2026-11-13: Snake 4465, Fox 3575,
# Wickedness 62725). The first joiner's arrival then dropped the host's real session: its pings became "unknown", the
# relay could no longer steer it, so no further hole was opened on that host and whoever needed one failed ("game no longer
# available": 21 failed joins, all on a hole the host had never pinged - usually the third player, sometimes the second).
# Now a NAT client that has made NAT requests (a real session) keeps its packets on the SRP path.
SRP_BY_SESSION = _env_on("SCCT_RELAY_SRP_BY_SESSION", "1")
# Reuse only quiet holes of other players (SCCT_RELAY_REUSE_OTHER_QUIET_S, default 15; 0 = off). 15 s is NOT the game's
# connection timeout: the game's settings (ON_LINS.UMD, ShadowStrikePS2.ini [IpDrv.TcpNetDriver]) have
# ConnectionTimeout=500 s, InitialConnectTimeout=60 s, KeepAliveTime=0.2 s. A live link sends at least every 0.2 s apart
# from pauses of a few seconds right after a join or map load (~6 s seen, below), so a host quiet on a hole for 15 s has
# stopped using that link; the host's side would not time it out by itself for 500 s. Reusing an open hole whose occupant link is idle is safe for that same player (a rematch), but another
# player's idle link may still be alive on the host's side. 2026-11-13 23:40:47: Fox's join was given hole 45001, which
# Snake had joined through 8 s before (his link pauses ~6 s right after a join); the join failed, Snake's link was moved
# to another hole and he fell out of the room 20 s later. 2026-11-14 00:19:50: Snake's rejoin was put on Wickedness's
# previous-map hole 45009 (host sent there 6 s before) and failed. Such a hole is now reused only after the host has
# been quiet on it.
REUSE_OTHER_QUIET_S = float(os.environ.get("SCCT_RELAY_REUSE_OTHER_QUIET_S", "15"))
# Rebinding follow (SCCT_RELAY_REBIND_FOLLOW, default on). At back to the lobby every joiner opens a new link, sends ~45
# packets, pauses ~7.7 s (loading) and carries on from the SAME socket (ANDROID-DOR and LINEBACKER at 04:18 on 2026-11-15).
# Twice LINEBACKER (PCSX2 Sockets mode over a VPN) came back from a NEW port after that pause (04:23:48, 05:20:26), the
# relay took it for a new link and put it on his old room link's hole, so the host's game saw the paused connection vanish
# and him appear at another relay address; 5 s later his game left the room (booted to the online lobby, the others went
# on). Now when the player's newest link, at most REBIND_MAX_AGE_S old when it paused, has been quiet 5-REBIND_S seconds
# and the same IP shows up from a new port, the new port takes over that link's hole.
REBIND_FOLLOW = _env_on("SCCT_RELAY_REBIND_FOLLOW", "1")
REBIND_S = float(os.environ.get("SCCT_RELAY_REBIND_S", "15"))
REBIND_MAX_AGE_S = float(os.environ.get("SCCT_RELAY_REBIND_MAX_AGE_S", "30"))
# No-reply move (SCCT_RELAY_NOREPLY_MOVE, default on). A player link that (re)starts on a hole and gets NO game data back
# through it for NOREPLY_S, while the host is sending game data on another hole right now, is on a hole that does not work
# for this host (2026-11-13 04:04: host's packets to 45001 never arrived; 04:30: relay packets from 45006 never reached the
# host's PC, Wireshark). It is moved, with any other unanswered link of that player on the same hole, to a working hole, and
# the old hole is marked bad for this host. Only links the host has never answered are moved (the host has no peer for
# them yet), so this cannot split a live conversation the way the old leave reset did.
NOREPLY_MOVE = _env_on("SCCT_RELAY_NOREPLY_MOVE", "1")
NOREPLY_S = float(os.environ.get("SCCT_RELAY_NOREPLY_S", "8"))
NOREPLY_MIN_PKTS = int(os.environ.get("SCCT_RELAY_NOREPLY_MIN_PKTS", "10"))
NOREPLY_HOST_ALIVE_S = float(os.environ.get("SCCT_RELAY_NOREPLY_HOST_ALIVE_S", "3"))   # host sent game data elsewhere this recently
NOREPLY_MAX_MOVES = int(os.environ.get("SCCT_RELAY_NOREPLY_MAX_MOVES", "2"))           # per link connection
# Failed steer (on with NOREPLY_MOVE). A steer whose ping has not arrived after STEER_FAIL_S while the console's other
# sockets keep pinging marks that hole bad and the ping is steered to another one. 2026-11-13 04:05:09: steer #10 never got
# a ping and the relay waited on it for two minutes, sending every rejoin to that dead hole.
STEER_FAIL_S = float(os.environ.get("SCCT_RELAY_STEER_FAIL_S", "60"))
BAD_HOLE_S = float(os.environ.get("SCCT_RELAY_BAD_HOLE_S", "600"))   # a bad hole is avoided for this host this long
# Rescue without waiting (SCCT_RELAY_RESCUE_NOWAIT, default on). The no-reply move used to wait up to STEER_FAIL_S (60 s) for
# any pending steer to the link's hole. 2026-11-15 04:59:52 (COOLDOR host, no forward, Cinema revenge swap): LINEBACKER's
# room link came back on hole 45003 (last pinged 70 s before, gone), the relay rescue-steered the ping there at 04:59:53 -
# but the link had been sending into 45003 for a second already, and on that router relay packets that reach a hole first
# block the host's ping (2026-11-14). The ping never came, the move waited for it, and LINEBACKER's game gave up on the room
# at 05:00:50 (booted to the online lobby), three seconds before the steer was declared failed. ANDROID-DOR's link, in the
# same spot on 45001 but with no pending steer, was moved to a hole the host had answered 7 s before and was fine 4 s later.
# Now a steer made AFTER the link was already sending into that hole is not waited for when another hole has carried
# the host's game data in the last HOLE_REUSE_S: the link moves there as ANDROID-DOR's did. A steer made before the link
# started (the hole was clean when the ping was asked for) is still waited for, as before.
RESCUE_NOWAIT = _env_on("SCCT_RELAY_RESCUE_NOWAIT", "1")
# Rescue fresh (SCCT_RELAY_RESCUE_FRESH, default on): the no-wait move above may also take a hole the host pinged within
# HOLD_PING_OPEN_S on a strict host (open on its router, nothing sent into it yet), not only one it answered on.
RESCUE_FRESH = _env_on("SCCT_RELAY_RESCUE_FRESH", "1")
HOST_DATA: dict = {}       # host ip -> {hole port: time of the last GAME datagram (not a ping) from the host on it}
BAD_HOLES: dict = {}       # host ip -> {hole port: time it was marked bad}
# Hold until pinged (SCCT_RELAY_HOLD_UNPINGED: auto (default) | on | off). 2026-11-14 22:08-22:13, COOLDOR hosting on PCSX2
# (PCAP bridged) with his router's port forward OFF: four new holes (45009, 45010, 45006, 45005) each got a joiner's packets
# 1-5 s after their steer, BEFORE the console's ping - and the console's ping never arrived on any of them, while its other
# sockets kept pinging. The one hole that worked (45001) had been pinged before anything was sent into it. With the forward
# ON (21:31-21:37, room 109) the same console pinged every new hole within one 25 s cycle. Reading: on a router with no
# forward, relay packets that reach a hole first leave a NAT entry that then blocks the console's own ping to that hole
# (until it expires, ~30 s after the last such packet). So for such a host ("strict"), a joiner's packets into a COLD hole
# (host silent on it > HOLD_COLD_S, or never) are held - dropped - until the host's ping has opened it. A join's first try
# can fail; a retry goes through once the ping arrives (within one 25 s cycle). auto: a host becomes "strict" when a link
# sent HOLD_DETECT_PKTS+ packets into a cold hole for HOLD_DETECT_S with no game data back while the host was sending game
# data on another hole; it becomes "open" when it answers on a cold hole (port forward or public IP: joins stay instant).
# The state survives re-logins and is cleared by a server restart. on: every host is strict. off: the old behaviour.
HOLD_MODE = os.environ.get("SCCT_RELAY_HOLD_UNPINGED", "auto").strip().lower()
HOLD_MODE = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off", "": "off"}.get(HOLD_MODE, HOLD_MODE)
# Resume ignore (SCCT_RELAY_HOLD_RESUME_IGNORE, default on): an answer that is part of the host's first burst after a
# silence (HOLD_RESUME_S) does not make the host "open" (see hold_detect).
HOLD_RESUME_IGNORE = os.environ.get("SCCT_RELAY_HOLD_RESUME_IGNORE", "1").strip().lower() not in ("0", "off", "no", "false", "")
HOLD_RESUME_S = 3.0
# Stricter "open" verdict (SCCT_RELAY_OPEN_STRICT, default OFF - nothing changes until it is set to 1). 2026-11-16 23:17:04:
# COOLDOR (strict ASUS) was judged open from an answer in the host's first burst after a map load, because the player's
# packet into that hole came a few ms AFTER the burst began and the burst guard above only looked at bursts that began
# after the first packet. The false mark lasted until the server restarted; at 2026-11-17 01:44 two returns then waited
# 35 s and 52 s without holds or the spare before the no-reply move corrected it. With this on:
#  - an answer within HOLD_RESUME_S of the start of the host's burst is ignored whichever came first;
#  - OPEN_PROOF clean answers (outside any burst, on different first packets) are needed before a host is called open;
#  - a clean answer must arrive within OPEN_CLEAN_S of the player's real first packet on that hole. 2026-11-17 02:37:40:
#    the probe timer is dropped and started again on a hole the host has used before, so an answer 16 s after the
#    players returned was logged as 3.9 s and 0.3 s and counted. The first-packet time is kept across that reset.
# It can only delay or prevent an "open" verdict; the strict verdict, holds and the spare pool are untouched.
OPEN_STRICT = os.environ.get("SCCT_RELAY_OPEN_STRICT", "0").strip().lower() not in ("0", "off", "no", "false", "")
OPEN_PROOF = max(1, int(os.environ.get("SCCT_RELAY_OPEN_PROOF", "2")))
OPEN_CLEAN_S = float(os.environ.get("SCCT_RELAY_OPEN_CLEAN_S", "2"))
# A player's run of packets into a cold hole counts as one run (one "real first packet") unless it pauses this long.
# Not LINK_IDLE_S (5 s): a returning player can pause longer than that around a map load, and a reset first-packet
# time would let a late answer look fast again. 30 s = the probe's own limit in hold_detect.
OPEN_RUN_GAP_S = 30.0
if HOLD_MODE not in ("auto", "on", "off"):
    HOLD_MODE = "auto"
HOLD_COLD_S = float(os.environ.get("SCCT_RELAY_HOLD_COLD_S", "170"))       # a used UDP mapping lasts ~180 s on home routers
# Cold age per strict host (SCCT_RELAY_HOLD_COLD_STRICT_S, default 60; 0 = always HOLD_COLD_S). 170 s assumed a used
# mapping lives ~180 s. 2026-11-15, COOLDOR (no forward, UPnP off), every session: a link that came back on a used hole
# after 70 s+ of host silence was never answered on that hole - the hole was gone, the link's own packets then blocked the
# host's ping there, and only a later move saved it or the player was dropped (06:40:07 Factory revenge swap: ANDROID-DOR's
# game link back on hole 45007, quiet 84 s, no reply for 65 s, spectator). So on a strict host a hole counts as cold after
# HOLD_COLD_STRICT_S of host silence: the link is held until the host's ping opens its hole, or moved to a ready spare at
# once. Fixed, not grown from evidence: the spare pool tried that (answered at 65-71 s, then failed at 39-42 s).
HOLD_COLD_STRICT_S = float(os.environ.get("SCCT_RELAY_HOLD_COLD_STRICT_S", "60"))


def cold_age(host: str) -> float:
    if HOLD_COLD_STRICT_S <= 0 or hold_state(host) != "strict":
        return HOLD_COLD_S
    return min(HOLD_COLD_S, HOLD_COLD_STRICT_S)


HOLD_DETECT_S = float(os.environ.get("SCCT_RELAY_HOLD_DETECT_S", "4"))     # an open host answers a new link within ~0.1 s
HOLD_DETECT_PKTS = int(os.environ.get("SCCT_RELAY_HOLD_DETECT_PKTS", "3"))
# Only a link made within this many seconds of its player's JOIN_ROOM can show that a host is strict: at a join an open
# host answers a new link at once (the join gives up after ~5 s), but a reconnect after a map can wait on the host's
# load (2026-11-15 00:41:23: DORIAN_D, public IP, did not answer SNAKE's reconnect in 4 s and was wrongly judged strict).
HOLD_JOIN_WINDOW_S = float(os.environ.get("SCCT_RELAY_HOLD_JOIN_WINDOW_S", "20"))
HOLD_DETECT_LATE_S = float(os.environ.get("SCCT_RELAY_HOLD_DETECT_LATE_S", "12"))   # the same test outside a join
HOLD_CLEAN_S = float(os.environ.get("SCCT_RELAY_HOLD_CLEAN_S", "35"))      # relay sent into a cold hole this recently: not a clean spare
# A hole the host has only PINGED never carries a reply on its own flow (the relay acks pings to the host's 10070), so the
# host's NAT keeps it only ~30 s after the last ping. 2026-11-14 23:07-23:16 (beta, COOLDOR, no forward): five reconnects at
# map loads were sent into spares pinged 36-58 s before; none was answered (each moved by the no-reply move after ~18 s).
HOLD_PING_OPEN_S = float(os.environ.get("SCCT_RELAY_HOLD_PING_OPEN_S", "28"))
HOST_HOLD: dict = {}       # host ip -> {"state": "unknown" | "strict" | "open", "probe": {hole: [first send, packets, endpoint]}}
RELAY_COLD_SEND: dict = {} # host ip -> {hole: last time the relay sent into it while it was cold (what blocks the next ping)}


# Downgrade (SCCT_RELAY_HOLD_DOWNGRADE, default on): a host judged open is judged strict after all when a link of it gets
# no reply on its hole while the host answers others (the no-reply move). 2026-11-15 06:00:02 COOLDOR (forward and UPnP
# off) answered a never-pinged hole in 0.1 s and was judged open; at 06:04:30 a hole went unanswered 52 s, and at the
# 06:19:41 Mount Hospital revenge swap the room links came back early into holes whose pings were due, blocked them, and
# DORIAN_D and ANDROID-DOR were dropped. His router does both. A host that reported its public address (no router) stays
# open.
HOLD_DOWNGRADE = os.environ.get("SCCT_RELAY_HOLD_DOWNGRADE", "1").strip().lower() not in ("0", "off", "no", "false", "")


def hold_downgrade(host: str, why: str) -> None:
    st = HOST_HOLD.get(host)
    if HOLD_MODE != "auto" or not HOLD_DOWNGRADE or st is None or st.get("state") != "open" or st.get("public"):
        return
    st["state"] = "strict"
    st.pop("probe", None)
    log(f"HOLD: host {host} was judged open but {why}: strict after all - from now on a joiner waits on a cold hole "
        f"until the host's ping opens it")
    upnp_open_shadow(host, f"downgraded from open: {why}")


# No-reply verdict (SCCT_RELAY_HOLD_NOREPLY_STRICT, default on): a host still untested ("unknown") is judged strict when
# a link of it gets no reply on a hole that was dead when the link started - no game data from the host there for
# HOLD_COLD_STRICT_S and no ping for HOLD_PING_OPEN_S - for NOREPLY_S while the host answers others (the no-reply move).
# An open router answers a link on any hole at once, whatever its age; a strict one has dropped such a hole. 2026-11-15
# 16:02-16:14, COOLDOR (no forward, UPnP off): the first test was rightly set aside (the host's first burst after a load),
# no other test came for 12 minutes, and meanwhile at 16:08:09 DORIAN_D's room link went straight into hole 45002 (host
# silent there 323 s, last ping 59 s before) and got no reply for 27 s - just 14 s before the next match started. A hole
# the host pinged recently does not count (no reply there means some other fault, not a strict NAT).
HOLD_NOREPLY_STRICT = os.environ.get("SCCT_RELAY_HOLD_NOREPLY_STRICT", "1").strip().lower() not in ("0", "off", "no", "false", "")


def hold_noreply_verdict(host: str, hole: int, start: float, now: float, who: str) -> None:
    """Called by the no-reply move once a link has gone unanswered on `hole` while the host answers others."""
    if HOLD_MODE != "auto" or not HOLD_NOREPLY_STRICT or hold_state(host) != "unknown":
        return
    age_limit = HOLD_COLD_STRICT_S if HOLD_COLD_STRICT_S > 0 else HOLD_COLD_S
    data_age = start - HOST_DATA.get(host, {}).get(hole, -1e9)
    ping_age = start - HOST_TRAFFIC.get(host, {}).get(hole, -1e9)
    if data_age <= age_limit or ping_age <= HOLD_PING_OPEN_S:
        return                     # the hole was not dead when the link started: no reply there proves nothing about the NAT
    st = HOST_HOLD.setdefault(host, {"state": "unknown"})
    st["state"] = "strict"
    st.pop("probe", None)
    ages = f"host silent there {'never used' if data_age > 1e8 else f'{data_age:.0f}s'}, " \
           f"last ping {'never' if ping_age > 1e8 else f'{ping_age:.0f}s'} before the link started"
    log(f"HOLD: host {host} is strict (no port forward?): {who} got no reply for {now - start:.0f}s on hole {hole}, which "
        f"was dead when the link started ({ages}), while the host answered others. From now on a joiner waits on a cold "
        f"hole until the host's ping opens it")
    upnp_open_shadow(host, f"no reply on dead hole {hole} for {now - start:.0f}s")
    pause_shadow_note(host, hole, now, f"no reply on dead hole {hole} for {now - start:.0f}s")


# ---- UPnP-open shadow (SCCT_RELAY_UPNP_OPEN_SHADOW, default OFF; LOG ONLY: no verdict or packet changes) ----
# 2026-11-21 01:19:16 COOLDOR (PCSX2 Sockets, SCOPS window V8.16: UPnP 12/12 confirmed by the ASUS router, Windows
# firewall rule in place) was judged strict when ONE link got no reply on a stale hole (45002, host silent 144 s), although
# the server's router probe reached his PC from fresh ports 4/4 and he had answered two holes he never pinged. With a
# forward the router cannot block that hole, so the silence was the game, not the NAT; the strict holds then added 16-27 s
# waits. Same pattern 2026-11-16. These lines say when a host is judged strict although its own SCOPS window reported a
# working forward and fresh router probes got through - data for a rule that would trust that evidence.
UPNP_OPEN_SHADOW = os.environ.get("SCCT_RELAY_UPNP_OPEN_SHADOW", "0").strip().lower() in ("1", "on", "true", "yes")
UPNP_OPEN_HINT_S = float(os.environ.get("SCCT_RELAY_UPNP_OPEN_HINT_S", "1800"))   # the window resends it every 20 min
UPNP_OPEN_STATS = {"strict": 0, "would_open": 0}


def upnp_open_shadow(host: str, why: str) -> None:
    """Called right after a host is judged strict. Log only."""
    if not UPNP_OPEN_SHADOW:
        return
    try:
        now = time.monotonic()
        UPNP_OPEN_STATS["strict"] += 1
        hint = SHADOW_HINT.get(host)
        pr = (SHADOW_EV.get(host) or {}).get("probe") or {}
        fok, fmiss = int(pr.get("fresh_ok", 0)), int(pr.get("fresh_miss", 0))
        hint_ok = hint is not None and now - hint <= UPNP_OPEN_HINT_S
        hname = NAT_NAMES.get(host)
        name = f"{hname} {host}" if hname else host
        hint_txt = f"{now - hint:.0f}s ago" if hint is not None else "none"
        if hint_ok and fok >= 1:
            UPNP_OPEN_STATS["would_open"] += 1
            log(f"UPNP_OPEN shadow: host {name} judged strict ({why}), but its SCOPS window reported a working UPnP forward "
                f"{hint_txt} and {fok}/{fok + fmiss} fresh router probes reached it: an open-host rule WOULD keep it open "
                f"(no holds) and move an unanswered link to a fresh hole instead. This run: strict verdicts "
                f"{UPNP_OPEN_STATS['strict']}, would stay open {UPNP_OPEN_STATS['would_open']} (log only)")
        else:
            log(f"UPNP_OPEN shadow: host {name} judged strict ({why}); evidence of a forward: UPnP report {hint_txt}, "
                f"fresh router probes {fok}/{fok + fmiss} seen -> strict stands (log only)")
    except Exception as e:  # logging must never affect routing
        log(f"UPNP_OPEN shadow error: {e!r}")


# Pause shadow (SCCT_RELAY_PAUSE_SHADOW, log only). 2026-11-21 02:46:44 and 03:20:29, COOLDOR (PCSX2 Sockets, manual
# forward, later also a Windows firewall rule; 4 s earlier at 03:20:07 he had answered an unsolicited packet on a
# never-pinged hole in 0.1 s, so the forward worked): both times he was judged strict because a joiner's match-start link
# (console port 10073) got nothing back on a cold hole for 4 s "while the host was sending on other holes" - but the
# host's last data elsewhere was ~2 s before the verdict, and it then answered NOBODY for 7 s, not even links on holes it
# was pinging (DORIAN_D's 10073 link on a warm hole waited 9.4 s too). That is the console pausing just before a match
# starts, not a router blocking. This watches every strict verdict for PAUSE_CHECK_S and logs whether the host went
# quiet on all its other holes for >= PAUSE_GAP_S in that time: if so, a pause-aware rule WOULD have cancelled the
# verdict and tested again later. Routing does not change.
PAUSE_SHADOW = os.environ.get("SCCT_RELAY_PAUSE_SHADOW", "0").strip().lower() in ("1", "on", "true", "yes")
PAUSE_CHECK_S = float(os.environ.get("SCCT_RELAY_PAUSE_CHECK_S", "8"))
PAUSE_GAP_S = float(os.environ.get("SCCT_RELAY_PAUSE_GAP_S", "3"))
PAUSE_WATCH = {}
# Loop timing (SCCT_RELAY_LOOP_STATS, off in code; log only). The relay is one loop: housekeeping runs on every pass,
# and a multi-packet NAT reply sleeps GAP_S between packets, during which nothing else is forwarded. This measures, per
# window of LOOP_STATS_S: how long each relayed game packet spent inside the server (from the loop noticing it to its
# send), the housekeeping time per pass, and every in-loop pause (count, total, and whether packets arrived during it
# and had to wait). Game packets = from a joiner on a relay port, or from a relayed host (its pings count too). One "LOOP STATS" line per window with traffic; nothing changes in routing or timing.
LOOP_STATS = os.environ.get("SCCT_RELAY_LOOP_STATS", "0").strip().lower() in ("1", "on", "true", "yes")
LOOP_STATS_S = float(os.environ.get("SCCT_RELAY_LOOP_STATS_S", "300"))
LOOP_BUCKETS_MS = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 25.0, 50.0)


class LoopStats:
    def __init__(self, now: float):
        self.reset(now)

    def reset(self, now: float) -> None:
        self.t0 = now
        self.passes = 0
        self.hk_sum = 0.0
        self.hk_max = 0.0
        self.game = [0] * (len(LOOP_BUCKETS_MS) + 1)   # in-server time of relayed game packets, histogram
        self.game_n = 0
        self.game_sum = 0.0
        self.game_max = 0.0
        self.other_n = 0
        self.other_max = 0.0
        self.pauses = 0
        self.pause_sum = 0.0
        self.pause_max = 0.0
        self.pause_waiting = 0       # pauses at whose end packets were queued on a socket (they waited for it)
        self.pause_game_waiting = 0  # ... of which a joiner relay (game) socket had data waiting

    def housekeeping(self, dt: float) -> None:
        self.passes += 1
        self.hk_sum += dt
        if dt > self.hk_max:
            self.hk_max = dt

    def packet(self, game: bool, dt: float) -> None:
        if game:
            ms = dt * 1000.0
            k = 0
            while k < len(LOOP_BUCKETS_MS) and ms >= LOOP_BUCKETS_MS[k]:
                k += 1
            self.game[k] += 1
            self.game_n += 1
            self.game_sum += dt
            if dt > self.game_max:
                self.game_max = dt
        else:
            self.other_n += 1
            if dt > self.other_max:
                self.other_max = dt

    def pause(self, dt: float, waiting: bool, game_waiting: bool) -> None:
        self.pauses += 1
        self.pause_sum += dt
        if dt > self.pause_max:
            self.pause_max = dt
        if waiting:
            self.pause_waiting += 1
        if game_waiting:
            self.pause_game_waiting += 1

    def pct(self, q: float) -> str:
        """Upper edge of the bucket holding the q-quantile of game packets."""
        if not self.game_n:
            return "-"
        need = q * self.game_n
        acc = 0
        for k, c in enumerate(self.game):
            acc += c
            if acc >= need:
                return f"<{LOOP_BUCKETS_MS[k]:g}ms" if k < len(LOOP_BUCKETS_MS) else f">={LOOP_BUCKETS_MS[-1]:g}ms"
        return "-"

    def line(self, now: float) -> str | None:
        if not (self.game_n or self.other_n or self.pauses):
            return None
        span = now - self.t0
        slow = sum(self.game[k] for k in range(len(self.game)) if k >= LOOP_BUCKETS_MS.index(5.0) + 1)
        return (f"LOOP STATS {span:.0f}s: game pkts {self.game_n} in server avg {self.game_sum / max(1, self.game_n) * 1000:.2f}ms "
                f"p99 {self.pct(0.99)} max {self.game_max * 1000:.1f}ms, >=5ms {slow}; other pkts {self.other_n} "
                f"max {self.other_max * 1000:.1f}ms; housekeeping {self.passes} passes avg "
                f"{self.hk_sum / max(1, self.passes) * 1000:.2f}ms max {self.hk_max * 1000:.1f}ms; pauses {self.pauses} "
                f"total {self.pause_sum * 1000:.0f}ms max {self.pause_max * 1000:.0f}ms, with packets waiting "
                f"{self.pause_waiting} (game {self.pause_game_waiting})")

    def tick(self, now: float) -> None:
        if now - self.t0 >= LOOP_STATS_S:
            ln = self.line(now)
            if ln:
                log(ln)
            self.reset(now)


# Pause-aware verdict (SCCT_RELAY_PAUSE_AWARE, off in code). The cold-hole test above no longer calls a host strict
# the moment a link has gone 4 s unanswered: it first waits up to PAUSE_CONFIRM_S for the host to send game data to
# anyone else. Seen -> the host is busy but ignoring this hole: strict, as before (a real strict host keeps sending to
# its other players, so this costs it well under a second). Not seen -> the host is pausing everywhere (2026-11-21
# COOLDOR, three false verdicts, each in a ~7 s pause): the verdict is cancelled and the host stays untested. At most
# PAUSE_MAX_CANCELS cancels per host per run (cancels within PAUSE_GROUP_S of each other are one pause and count once);
# after that the old immediate verdict applies.
PAUSE_AWARE = os.environ.get("SCCT_RELAY_PAUSE_AWARE", "0").strip().lower() in ("1", "on", "true", "yes")
PAUSE_CONFIRM_S = float(os.environ.get("SCCT_RELAY_PAUSE_CONFIRM_S", "3"))
PAUSE_MAX_CANCELS = int(os.environ.get("SCCT_RELAY_PAUSE_MAX_CANCELS", "2"))
PAUSE_GROUP_S = float(os.environ.get("SCCT_RELAY_PAUSE_GROUP_S", "10"))   # cancels this close together = one pause
PAUSE_STATS = {"strict": 0, "paused": 0}


def pause_shadow_note(host: str, port, now: float, why: str) -> None:
    """Called right after a host is judged strict. Log only."""
    if not PAUSE_SHADOW:
        return
    try:
        others = [t for p, t in HOST_DATA.get(host, {}).items() if p != port]
        last = max(others) if others else None
        PAUSE_STATS["strict"] += 1
        PAUSE_WATCH[host] = {"t": now, "port": port, "why": why,
                             "gap": (now - last) if last is not None else 0.0, "last0": last}
    except Exception as e:  # logging must never affect routing
        log(f"PAUSE shadow error: {e!r}")


def pause_shadow_tick(now: float) -> None:
    if not PAUSE_SHADOW or not PAUSE_WATCH:
        return
    try:
        for host, w in list(PAUSE_WATCH.items()):
            others = [t for p, t in HOST_DATA.get(host, {}).items() if p != w["port"]]
            last = max(others) if others else None
            if last is not None:
                w["gap"] = max(w["gap"], now - last)
            if now - w["t"] < PAUSE_CHECK_S:
                continue
            PAUSE_WATCH.pop(host, None)
            hname = NAT_NAMES.get(host)
            name = f"{hname} {host}" if hname else host
            lead = f"{w['t'] - w['last0']:.1f}s" if w["last0"] is not None else "never"
            if w["gap"] >= PAUSE_GAP_S:
                PAUSE_STATS["paused"] += 1
                log(f"PAUSE shadow: host {name} judged strict ({w['why']}), but in the {PAUSE_CHECK_S:.0f}s around it the host "
                    f"sent nothing on any other hole for {w['gap']:.1f}s (last data {lead} before the verdict): the host was "
                    f"pausing (e.g. a match about to start), not blocking - a pause-aware rule WOULD cancel this verdict and "
                    f"test again later. This run: strict verdicts {PAUSE_STATS['strict']}, during a host pause "
                    f"{PAUSE_STATS['paused']} (log only)")
            else:
                log(f"PAUSE shadow: host {name} judged strict ({w['why']}); it kept sending on other holes (longest gap "
                    f"{w['gap']:.1f}s in {PAUSE_CHECK_S:.0f}s) -> verdict stands (log only)")
    except Exception as e:  # logging must never affect routing
        log(f"PAUSE shadow error: {e!r}")


def hold_state(host: str) -> str:
    if HOLD_MODE == "on":
        return "strict"
    if HOLD_MODE == "off":
        return "open"
    return HOST_HOLD.get(host, {}).get("state", "unknown")


# Spare pool (SCCT_RELAY_SPARE_POOL=N, 0 = off; only for hosts judged strict). A strict host opens one new hole per 25 s
# ping cycle, so when three joiners reconnect at a map change the third waited 53-59 s and the round started without it
# (2026-11-15: spectator at 01:13, "game no longer available" at 02:02/02:04, hosts SNAKE and StandaloneF0X, no forward).
# What the same logs show: a hole the host only pinged is gone ~30 s later (no packet ever came back on that flow), but a
# hole that carried packets BOTH ways was still open after 60-116 s of silence, and one stayed open 381 s on relay
# keepalives plus a host ping every 75-100 s. So right after the host's ping lands on a hole the relay sends two small
# packets back through it (POOL_ASSURE_GAP_S apart: a router marks a UDP entry as two-way on the first reply and gives it
# the long timeout on the next packet), then one every POOL_KA_S. N such spares are kept ready; a new or reconnecting
# link whose own hole is cold takes one at once instead of waiting for the next ping.
# v1 of this tried to LEARN each host's real lifetime (grow a trust window from a ping or a link that was answered late,
# shrink on a miss). 2026-11-15 real test (COOLDOR, no forward, UPnP off): that backfired. A link was answered fine at
# 65 s and 71 s of silence, then another failed at just 39 s and another at 42 s - failures and successes interleaved
# at similar ages, so there was no safe age to learn. Reading: the router's own idle-UDP-mapping behaviour here is not a
# simple function of age (more likely a fixed-interval sweep the keepalives cannot reliably dodge), so nothing is grown
# from evidence any more. POOL_TRUST_S is a fixed, deliberately modest ceiling.
# 2026-11-15 23:59 FOX (no forward): one kept spare was silent 34 s, just under that 35 s ceiling, and did not answer.
# POOL_MAX_MISSES=1 then switched the pool off for the rest of the session, and the Cinema returns waited 59-66 s.
# Step-down (POOL_STEPDOWN, default on): that first miss keeps the pool but trusts spares only for one ping cycle
# (POOL_TRUST_STEP_S, 20 s): the pool keeps steering the ping to an idle hole and moving a returning link straight onto
# it, but no longer relies on keepalives past a pinged hole's own life. A miss at a ceiling above HOLD_PING_OPEN_S
# (only if POOL_TRUST_STEP_S is set that high) still switches the pool off.
POOL_SIZE = int(os.environ.get("SCCT_RELAY_SPARE_POOL", "0") or 0)
POOL_KA_S = float(os.environ.get("SCCT_RELAY_POOL_KEEPALIVE_S", "20"))
POOL_ASSURE_GAP_S = 2.5         # between the two packets that make a pinged hole two-way
POOL_ASSURE_MAX_AGE_S = 20.0    # only a hole the host pinged this recently is certainly still there to be assured
POOL_TRUST_S = float(os.environ.get("SCCT_RELAY_POOL_TRUST_S", "35"))  # fixed ceiling; not grown from evidence (see above)
POOL_TRUST_STEP_S = float(os.environ.get("SCCT_RELAY_POOL_TRUST_STEP_S", "20"))  # after one miss: one host ping cycle
POOL_STEPDOWN = os.environ.get("SCCT_RELAY_POOL_STEPDOWN", "1") != "0"
POOL_TRIAL_S = 5.0              # a link on a kept spare must be answered within this (a join gives up after ~5 s)
POOL_SPARE_IDLE_S = 30.0        # a hole whose links have all been quiet this long can serve as a spare
POOL_MAX_MISSES = int(os.environ.get("SCCT_RELAY_POOL_MAX_MISSES", "1") or 1)
POOL: dict = {}                 # host ip -> state, see pool_st


def pool_st(host: str) -> dict:
    return POOL.setdefault(host, {"off": "", "misses": 0, "uses": 0, "logged": 0, "trust": POOL_TRUST_S,
                                  "life": {},     # hole -> [first relay packet after the host opened it, time it became two-way]
                                  "trial": {}})   # hole -> [time a link was put on this kept spare, endpoint, spare age]


def pool_trust(host: str) -> float:
    """How long a kept spare may sit with no host packet before it stops being offered. Full ceiling until the first
    miss, then one ping cycle (see POOL_STEPDOWN)."""
    st = POOL.get(host)
    if not st:
        return POOL_TRUST_S
    return float(st.get("trust", POOL_TRUST_S))


def pool_on(host: str) -> bool:
    return (POOL_SIZE > 0 and bool(RELAY_HOLES) and HOLD_MODE != "off" and hold_state(host) == "strict"
            and not POOL.get(host, {}).get("off"))


def pool_alive(host: str, port: int, now: float) -> bool:
    """A kept spare the relay may send a link into: made two-way after the host's ping, keepalives running, and the host's
    last packet on it no older than the fixed trust ceiling."""
    if not pool_on(host):
        return False
    lf = POOL[host]["life"].get(port) if host in POOL else None
    if not lf or lf[1] is None:
        return False
    return (now - HOST_TRAFFIC.get(host, {}).get(port, -1e9) <= pool_trust(host)
            and now - RELAY_TRAFFIC.get(host, {}).get(port, -1e9) <= POOL_KA_S + 10)


def pool_sent(host: str, port: int, now: float) -> None:
    """The relay sent a packet through an open hole of a pool host."""
    if not pool_on(host):
        return
    lf = pool_st(host)["life"]
    if port not in lf:
        lf[port] = [now, None]
    elif lf[port][1] is None and now - lf[port][0] >= POOL_ASSURE_GAP_S:
        lf[port][1] = now


def pool_retire(host: str, port: int, now: float) -> None:
    st = pool_st(host)
    st["life"].pop(port, None)
    st["trial"].pop(port, None)
    last = RELAY_TRAFFIC.get(host, {}).get(port)
    if last is not None and now - last < HOLD_CLEAN_S and hole_cold(host, port, now):
        cs = RELAY_COLD_SEND.setdefault(host, {})
        cs[port] = max(cs.get(port, -1e9), last)      # the relay's last packets may block a ping there for a while


def pool_miss(host: str, port: int, age: float, now: float, what: str) -> None:
    st = pool_st(host)
    pool_retire(host, port, now)
    st["misses"] += 1
    ceiling = float(st.get("trust", POOL_TRUST_S))
    # First miss at the full ceiling: do not turn the pool off. FOX 23:59:29 was one spare silent 34 s
    # (just under 35 s). Switching off left every later return waiting on the host's single ping.
    if POOL_STEPDOWN and ceiling > POOL_TRUST_STEP_S + 0.5 and st["misses"] == 1:
        st["trust"] = POOL_TRUST_STEP_S
        traffic = HOST_TRAFFIC.get(host, {})
        for p in list(st["life"]):
            if now - traffic.get(p, -1e9) > POOL_TRUST_STEP_S:
                pool_retire(host, p, now)
        log(f"POOL step down for host {host}: {what} (hole {port}, host silent {age:.0f}s). "
            f"Trust lowered {ceiling:.0f}s -> {POOL_TRUST_STEP_S:.0f}s (one ping cycle); the pool stays on. "
            f"Spares are now only holes the host pinged within that time")
        return
    if st["misses"] < POOL_MAX_MISSES:
        log(f"POOL: host {host}: {what} (hole {port}, host silent {age:.0f}s). Miss {st['misses']} of {POOL_MAX_MISSES}; "
            f"that spare is dropped")
        return
    st["off"] = what
    for p in list(st["life"]):
        pool_retire(host, p, now)
    log(f"POOL OFF for host {host}: {what} (hole {port}, host silent {age:.0f}s), {st['misses']} miss(es) at a {ceiling:.0f}s "
        f"ceiling. Keepalives do not reliably hold this router's holes open; it is handled as before (a joiner waits for "
        f"the host's ping) until the server restarts")


def pool_tick(now: float) -> None:
    """Per second: drop spares that are too old, and judge links that were put on a kept spare."""
    if POOL_SIZE <= 0:
        return
    for host, st in list(POOL.items()):
        traffic = HOST_TRAFFIC.get(host, {})
        if not pool_on(host):
            if st["life"]:
                for p in list(st["life"]):
                    pool_retire(host, p, now)
            continue
        hdata = HOST_DATA.get(host, {})
        for port, lf in list(st["life"].items()):
            age = now - traffic.get(port, -1e9)
            if (lf[1] is None and age > HOLD_PING_OPEN_S) or age > pool_trust(host):
                pool_retire(host, port, now)
        hv = RELAY_HOSTS.get(host)
        for port, (t0, ep, sage) in list(st.get("trial", {}).items()):
            if hdata.get(port, -1e9) >= t0:
                st["trial"].pop(port, None)
                st["uses"] += 1
                if st["uses"] <= 5 or st["uses"] % 20 == 0:
                    log(f"POOL: host {host} answered {_who(ep[0])}:{ep[1]} on kept spare {port} {hdata[port] - t0:.1f}s after "
                        f"its first packet (the host had been silent there {sage:.0f}s) - use #{st['uses']}")
                continue
            if now - t0 < POOL_TRIAL_S:
                continue
            st["trial"].pop(port, None)
            sending = hv is not None and ep in hv["eps"] and now - hv["eps"][ep][1] <= 1.5 and hv.get("src", {}).get(ep) == port
            alive = any(p != port and now - t <= 3.0 for p, t in hdata.items())
            if sending and alive:
                pool_miss(host, port, sage + now - t0, now, f"{_who(ep[0])}:{ep[1]} got no answer on a kept spare in "
                          f"{now - t0:.0f}s while the host was sending on other holes")


def pool_spare(hv: dict, host: str, ep, now: float, skip=None):
    """A ready spare for endpoint ep (a hole the host pinged a moment ago, or a kept one), or None."""
    src = hv.get("src", {})
    traffic = HOST_TRAFFIC.get(host, {})
    hdata = HOST_DATA.get(host, {})
    best, best_key = None, None
    for pt in RELAY_HOLES:
        if pt == skip or hole_bad(host, pt, now) or pt not in traffic:
            continue
        if not (pool_alive(host, pt, now) or now - traffic[pt] <= HOLD_PING_OPEN_S):
            continue
        occ = [e for e, p in src.items() if p == pt and e != ep]
        if any(holds_hole(hv, e, ep, now) for e in occ):
            continue
        other = any(e[0] != ep[0] for e in occ)
        if other and REUSE_OTHER_QUIET_S > 0 and now - hdata.get(pt, -1e9) < REUSE_OTHER_QUIET_S:
            continue
        key = (not other, traffic[pt])
        if best_key is None or key > best_key:
            best, best_key = pt, key
    return best


def pool_use(host: str, port: int, ep, now: float) -> None:
    """A link was put on a hole: if that relies on the keepalives (the host has been silent there longer than a pinged hole
    lasts), watch whether the host answers."""
    if not pool_on(host):
        return
    age = now - HOST_TRAFFIC.get(host, {}).get(port, -1e9)
    # Only a spare older than a pinged hole lasts (HOLD_PING_OPEN_S) relies on the keepalives, so only that is on trial.
    # After a step-down (20 s ceiling) no kept spare is that old: a spare is then just a hole the host pinged moments ago,
    # the same hole a joiner would wait for with the pool off, so a failure there says nothing against the pool and is
    # left to the no-reply move. (Grok's draft trialed 12-20 s spares; that could switch the pool off for no gain.)
    if age > HOLD_PING_OPEN_S and now - HOST_DATA.get(host, {}).get(port, -1e9) > HOLD_PING_OPEN_S:
        pool_st(host)["trial"][port] = [now, ep, age]


def pool_pick(clt, hv: dict, traffic: dict, now: float):
    """Where to steer the console's ping for the spare pool: open another spare to replace one that is ageing out of the
    fixed trust window. No re-ping-and-wait any more (see POOL_TRUST_S note above: age does not predict reliability here,
    so there is nothing to prove by re-pinging the same hole - a fresh one is just as good and costs no extra wait).
    (None, "") = nothing to do."""
    host = clt.addr
    st = pool_st(host)
    src = hv.get("src", {})
    eps = hv["eps"]
    def idle(pt):
        return all(e not in eps or now - eps[e][1] > POOL_SPARE_IDLE_S for e, p in src.items() if p == pt)
    members = 0
    for pt in RELAY_HOLES:
        if hole_bad(host, pt, now) or pt not in traffic or not idle(pt):
            continue
        age = now - traffic[pt]
        lf = st["life"].get(pt)
        kept = bool(lf) and lf[1] is not None and age <= pool_trust(host)
        if kept or age <= HOLD_PING_OPEN_S:
            members += 1
    if members < POOL_SIZE:
        have = {pt for pt in RELAY_HOLES if pt in traffic and idle(pt)
                and (now - traffic[pt] <= HOLD_PING_OPEN_S
                     or (st["life"].get(pt) and st["life"][pt][1] is not None and now - traffic[pt] <= pool_trust(host)))}
        assigned = set(src.values())
        cands = [pt for pt in RELAY_HOLES if pt not in have and pt != clt.hole_target and not hole_bad(host, pt, now)
                 and idle(pt) and relay_clean(host, pt, now)]
        if cands:
            pt = min(cands, key=lambda p: (p in assigned, traffic.get(p, 0.0)))
            return pt, f"pool: opening spare {members + 1} of {POOL_SIZE}"
    return None, ""


def hole_cold(host: str, port: int, now: float) -> bool:
    """The host's NAT probably has no live entry for this hole: no game data from the host through it for HOLD_COLD_S (an
    entry that carried traffic both ways lasts ~180 s) and no ping on it for HOLD_PING_OPEN_S (a ping-only entry ~30 s) -
    unless it is a kept spare of the pool (see SPARE_POOL)."""
    return (now - HOST_DATA.get(host, {}).get(port, -1e9) > cold_age(host)
            and now - HOST_TRAFFIC.get(host, {}).get(port, -1e9) > HOLD_PING_OPEN_S
            and not pool_alive(host, port, now))


def relay_clean(host: str, port: int, now: float) -> bool:
    """The relay has not sent into this hole while it was cold for HOLD_CLEAN_S (so the console's ping can open it)."""
    return now - RELAY_COLD_SEND.get(host, {}).get(port, -1e9) > HOLD_CLEAN_S


def hold_detect(now: float) -> None:
    """auto mode: decide per host whether packets sent into a cold hole are answered (open) or not (strict)."""
    if HOLD_MODE != "auto":
        return
    for host, st in HOST_HOLD.items():
        if st.get("state") != "unknown":
            continue
        pr = st.setdefault("probe", {})
        hd = HOST_DATA.get(host, {})
        hv = RELAY_HOSTS.get(host) or {}
        if st.get("pause_wait"):
            live = {(p, v[0]) for p, v in pr.items()}
            for k in [k for k in st["pause_wait"] if k not in live]:
                st["pause_wait"].pop(k, None)   # its test ended some other way
        for port, (t0, n, ep) in list(pr.items()):
            if hd.get(port, -1e9) >= t0:
                back = hv.get("host_active_since", -1e9)
                in_burst = hd[port] - back <= HOLD_RESUME_S and (back > t0 or OPEN_STRICT)
                if HOLD_RESUME_IGNORE and in_burst:
                    # The host came back from a silence (a map load) after the first packet went in, and this is part of
                    # its first burst: after a load the host's game sends to every player first, which opens the hole
                    # from its own side on any router. 2026-11-15 04:57:44 and 05:15:51: COOLDOR (no forward, UPnP off)
                    # was judged open exactly so, at the instant its match-start load ended; it is strict.
                    pr.pop(port, None)
                    log(f"HOLD: host {host}: hole {port} answered only in the host's first burst after a silence (map "
                        f"load, {hd[port] - t0:.0f}s after the first packet): proves nothing about its NAT; still testing")
                    continue
                if OPEN_STRICT:
                    real = t0
                    slot = st.get("link0", {}).get(port)
                    if slot and slot[2] == ep and slot[0] <= t0:
                        real = slot[0]
                    if hd[port] - real > OPEN_CLEAN_S:
                        pr.pop(port, None)
                        log(f"HOLD: host {host}: hole {port} answered {hd[port] - real:.1f}s after the player's first "
                            f"packet on it (probe timer {hd[port] - t0:.1f}s): too late to prove open; still testing")
                        continue
                if OPEN_STRICT and OPEN_PROOF > 1:
                    st["open_hits"] = st.get("open_hits", 0) + 1
                    if st["open_hits"] < OPEN_PROOF:
                        pr.pop(port, None)
                        log(f"HOLD: host {host} answered on hole {port} {hd[port] - t0:.1f}s after the first packet into it, "
                            f"before any ping there and outside a map-load burst: clean answer {st['open_hits']} of "
                            f"{OPEN_PROOF} needed for open; still testing")
                        continue
                st["state"] = "open"
                log(f"HOLD: host {host} answered on hole {port} {hd[port] - t0:.1f}s after the first packet into it, before "
                    f"any ping there: open NAT or port forward - joiners are sent through new holes at once"
                    + (f" ({OPEN_PROOF} clean answers)" if OPEN_STRICT and OPEN_PROOF > 1 else ""))
                break
            if HOST_TRAFFIC.get(host, {}).get(port, -1e9) >= t0 or now - t0 > 30:
                pr.pop(port, None)       # the host pinged it meanwhile, or no verdict in 30 s: no evidence either way
                continue
            if now - t0 < HOLD_DETECT_S or n < HOLD_DETECT_PKTS:
                continue
            mp = RELAY_MAP.get(ep[0], {})
            if mp.get("host") == host and t0 - mp.get("t", -1e9) > HOLD_JOIN_WINDOW_S:
                # Not a join (e.g. a reconnect after a map, which even an open host may answer late): only a hole the host
                # has never sent game data through counts, and only after HOLD_DETECT_LATE_S without an answer.
                if port in hd:
                    pr.pop(port, None)
                    continue
                if now - t0 < HOLD_DETECT_LATE_S:
                    continue
            pw = st.setdefault("pause_wait", {}) if PAUSE_AWARE else {}
            if (port, t0) in pw:
                # Pause-aware confirmation running (see PAUSE_AWARE): strict only once the host is seen sending to
                # someone else AFTER the check started while this hole stays unanswered.
                try:
                    p_start = pw[(port, t0)]
                    fresh = [t for p, t in hd.items() if p != port and t > p_start]
                    if not fresh:
                        if now - p_start < PAUSE_CONFIRM_S:
                            continue
                        pw.pop((port, t0), None)
                        pr.pop(port, None)
                        # One pause can catch several players' links at once (2026-11-21 05:21:27: two joiners who
                        # joined together, both cancelled, "2 of 2" used up by a single pause). Cancels within
                        # PAUSE_GROUP_S of the previous one belong to the same pause and count once.
                        same = now - st.get("pause_last_cancel", -1e9) <= PAUSE_GROUP_S
                        if not same:
                            st["pause_cancels"] = st.get("pause_cancels", 0) + 1
                        st["pause_last_cancel"] = now
                        others = [t for p, t in hd.items() if p != port]
                        quiet = now - max(others) if others else 0.0
                        log(f"HOLD: host {host}: no reply on cold hole {port}, but the host has sent nothing on any other "
                            f"hole for {quiet:.1f}s either: pausing (e.g. a match about to start), not blocking -> strict "
                            f"verdict cancelled ({st['pause_cancels']} of {PAUSE_MAX_CANCELS}"
                            + (", same pause as the previous cancel, not counted again" if same else "") + "); still testing")
                        continue
                    pw.pop((port, t0), None)   # host is talking to others but not on this hole: a real block
                except Exception as e:   # never let the check stop the relay: fall back to the old verdict
                    pw.pop((port, t0), None)
                    log(f"PAUSE check error (old verdict used): {e!r}")
            else:
                alive = [t for p, t in hd.items() if p != port and now - t <= 5.0]
                if not alive:
                    continue             # host not sending game data anywhere (loading a map): no verdict yet
                if PAUSE_AWARE and (st.get("pause_cancels", 0) < PAUSE_MAX_CANCELS
                                    or now - st.get("pause_last_cancel", -1e9) <= PAUSE_GROUP_S):
                    pw[(port, t0)] = now
                    log(f"HOLD: host {host}: no reply on cold hole {port} for {now - t0:.1f}s; checking for up to "
                        f"{PAUSE_CONFIRM_S:.0f}s that the host is still sending to others before calling it strict")
                    continue
            st["state"] = "strict"
            log(f"HOLD: host {host} is strict (no port forward?): {_who(ep[0])}:{ep[1]} sent {n} packets into cold hole {port} "
                f"in {now - t0:.1f}s and got nothing back while the host was sending on other holes. From now on a joiner "
                f"waits on a cold hole until the host's ping opens it, and the ping goes to holes nothing was sent into")
            upnp_open_shadow(host, f"nothing back on cold hole {port}")
            pause_shadow_note(host, port, now, f"nothing back on cold hole {port}")
            break
        if st.get("state") != "unknown":
            st.pop("probe", None)


def hole_bad(host: str, port, now: float) -> bool:
    t = BAD_HOLES.get(host, {}).get(port)
    return t is not None and now - t < BAD_HOLE_S


def mark_bad(host: str, port: int, now: float, why: str) -> None:
    active = [p for p in BAD_HOLES.get(host, {}) if hole_bad(host, p, now)]
    if len(active) >= len(RELAY_HOLES) // 2:
        log(f"HOLE bad: {host} hole {port} NOT marked ({len(active)} holes already bad for this host; {why})")
        return
    BAD_HOLES.setdefault(host, {})[port] = now
    log(f"HOLE bad: {host} hole {port} marked bad for {BAD_HOLE_S:.0f}s ({why})")


if WARM_HOLES and "SCCT_RELAY_HOLE_BUDGET" not in os.environ:
    HOLE_BUDGET = 100000   # warm steers run all session; the console takes them since the SRP wrap fix


def note_host_reply(host: str, port: int, now: float) -> None:
    """Call for game data from a host on a hole, BEFORE HOST_TRAFFIC is updated. If the host has sent nothing through this
    hole (no ping, no data) for longer than any mapping lasts, our packet reached it through a mapping that did not exist for
    this hole: its NAT filters by address only, so it needs no steering."""
    last = HOST_TRAFFIC.get(host, {}).get(port, 0.0)
    if last > 0.0 and now - last <= HOLE_OPEN_GAP_S:
        return
    sent = RELAY_TRAFFIC.get(host, {}).get(port, 0.0)
    if sent <= 0.0 or now - sent > HOLE_OPEN_WAIT_S:
        return    # not an answer to anything we just delivered
    st = HOST_NAT.setdefault(host, {"hits": 0, "misses": 0, "open": False})
    st["hits"] += 1
    st["misses"] = 0
    ev = f"{port} " + ("never pinged" if last <= 0.0 else f"last used {now - last:.0f}s before") + \
        f", answered {now - sent:.1f}s after a relay packet ({now_ts()[11:19]})"
    st.setdefault("ev", []).append(ev)
    del st["ev"][:-6]
    log(f"HOLE open-NAT evidence #{st['hits']}: {host} answered on hole {port}, which it " +
        ("never pinged" if last <= 0.0 else f"last used {now - last:.0f}s ago"))
    if not st["open"] and st["hits"] >= HOLE_OPEN_HITS:
        st["open"] = True
        log(f"HOLE adaptive: {host} looks address-restricted" + ("; rescue steers only after a long silence" if HOLE_ADAPTIVE else " (SCCT_RELAY_ADAPTIVE_STEER is off, steering unchanged)")
            + (f" | evidence: {'; '.join(st.get('ev', []))}. Each is the host answering on a hole it had not pinged lately; "
               f"a UPnP or manual forward to the console gives the same answer on a strict router" if FAIL_LOG else ""))


def _parse_port_list(spec: str) -> list[int]:
    out: list[int] = []
    for part in str(spec).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def relay_snapshot() -> None:
    if not RELAY_SNAPSHOT:
        return
    try:
        import json as _json
        p = pathlib.Path(RELAY_SNAPSHOT)
        p.parent.mkdir(parents=True, exist_ok=True)
        snap = {
            "map": RELAY_MAP,
            "hosts": {h: {"joiner": list(v["joiner"]), "relay_port": v["sock"].getsockname()[1], "rx": v["rx"], "tx": v["tx"]}
                      for h, v in RELAY_HOSTS.items()},
            "ts": now_ts(),
        }
        p.write_text(_json.dumps(snap, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"RELAY snapshot failed: {e!r}")


def relay_control(data: bytes, addr: tuple[str, int]) -> None:
    """Control messages from the lobby server (JSON, one per datagram, loopback only)."""
    import json as _json
    try:
        m = _json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        log(f"RELAY ctrl: bad message from {addr[0]}:{addr[1]}: {e!r}")
        return
    op = str(m.get("op", ""))
    if op == "map":
        j, h = str(m.get("joiner", "")), str(m.get("host", ""))
        if not j or not h:
            log(f"RELAY ctrl: map without joiner/host: {m}")
            return
        prev = RELAY_MAP.get(j)
        if prev is not None and (prev.get("host") != h or (prev.get("user") and m.get("user") and prev.get("user") != m.get("user"))):
            reset_joiner(j, f"re-mapped by {m.get('user')} (was {prev.get('user')} -> {prev.get('host')})")
        RELAY_MAP[j] = {"host": h, "room": m.get("room"), "user": m.get("user"), "ts": now_ts(), "t": time.monotonic()}
        RELAY_UNMAPPED_LOGGED.discard(j)
        log(f"RELAY ctrl: map joiner {j} ({m.get('user')}) -> host {h} room {m.get('room')}")
        lan = str(m.get("host_lan", "") or "")
        if lan and lan == h and HOLD_MODE == "auto" and HOST_HOLD.get(h, {}).get("state") != "open":
            # The host's console reported its public address as its own (LOBBYSERVERLOGIN): no router in front of it, so
            # nothing to hold for. 2026-11-15 00:41: DORIAN_D's PS2 on the modem was judged strict from a reconnect it
            # was slow to answer after a map, and a later join (StandaloneF0X) was held and failed.
            HOST_HOLD[h] = {"state": "open", "public": True}
            log(f"HOLD: host {h} reports its public address as its own (no router in front): open, joiners are sent at once")
        if LEAVE_GRACE and RELAY_HOLES:
            forget_idle_links(j, time.monotonic(), f"join room {m.get('room')}")
    elif op == "unmap":
        j = str(m.get("joiner", ""))
        ent = RELAY_MAP.pop(j, None)
        if ent is not None:
            h = ent["host"]
            hv = RELAY_HOSTS.get(h)
            if hv is not None and hv["joiner"][0] == j:
                RELAY_HOSTS.pop(h, None)
            log(f"RELAY ctrl: unmap joiner {j} (was -> {h})")
    elif op == "unmap_host":
        h = str(m.get("host", ""))
        gone = [j for j, e in RELAY_MAP.items() if e["host"] == h]
        for j in gone:
            RELAY_MAP.pop(j, None)
        RELAY_HOSTS.pop(h, None)
        log(f"RELAY ctrl: unmap host {h} (joiners dropped: {gone})")
    elif op == "unmap_any":
        ip = str(m.get("ip", ""))
        gone = [j for j, e in RELAY_MAP.items() if j == ip or e["host"] == ip]
        for j in gone:
            RELAY_MAP.pop(j, None)
        RELAY_HOSTS.pop(ip, None)
        for h, hv in list(RELAY_HOSTS.items()):
            if hv["joiner"][0] == ip:
                RELAY_HOSTS.pop(h, None)
        if gone:
            log(f"RELAY ctrl: unmap_any {ip} (entries dropped: {gone})")
    elif op == "reset_joiner":
        reset_joiner(str(m.get("joiner", "")), f"{m.get('why')} ({m.get('user')})")
    elif op == "clear":
        RELAY_MAP.clear()
        RELAY_HOSTS.clear()
        log("RELAY ctrl: cleared")
    else:
        log(f"RELAY ctrl: unknown op {op!r}: {m}")
    relay_snapshot()


def reset_joiner(j: str, why: str) -> None:
    """Forget a joiner IP's endpoints and slots in every host flow (the mapping itself stays), so a new console or a new
    session from that IP starts with free slots. Two consoles behind one NAT share an IP: the old one's established links
    otherwise keep all slots and the new one's links are crammed into one (2026-11-04 19:13)."""
    n = 0
    tnow = time.monotonic()
    if FAST_RESUME_LOG:
        fast_resume_leave(j, why, tnow)
    for h, hv in RELAY_HOSTS.items():
        eps = [e for e in hv["eps"] if e[0] == j]
        if SILENCE_LOG:
            for e in eps:
                if hv.get("ep_rx", {}).get(e, 0) < SILENCE_MIN_RX:
                    continue
                hole = hv.get("src", {}).get(e)
                ltx = HOST_TRAFFIC.get(h, {}).get(hole) if hole else None
                log(f"RELAY silence: {_who(j)}:{e[1]} at reset ({why}): last from player {tnow - hv['eps'][e][1]:.1f}s ago, "
                    f"last from host on hole {hole} " + (f"{tnow - ltx:.1f}s ago" if ltx else "never") +
                    f", packets player->host {hv.get('ep_rx', {}).get(e, 0)}, host->player {hv.get('ep_tx', {}).get(e, 0)}")
        kept = []
        for e in eps:
            if LEAVE_GRACE and RELAY_HOLES and tnow - hv["eps"][e][1] <= LEAVE_LIVE_S and e in hv.get("src", {}):
                # still sending: keep its hole until it goes quiet (leave_expire), so the host never sees it move
                hv.setdefault("leaving", {})[e] = tnow
                kept.append(e)
                continue
            _forget_ep(hv, e)
            n += 1
        if kept:
            log("RELAY host %s: %s still sending at the leave; keeps its hole until quiet %.0fs (%s)" % (
                h, ", ".join("%s:%s@%s" % (e[0], e[1], hv["src"][e]) for e in kept), LEAVE_IDLE_S, why))
        if hv["joiner"][0] == j and eps and not kept:
            hv["joiner"] = eps[0]   # stale, replaced by the next packet
    if n:
        log(f"RELAY ctrl: reset joiner {j}: {n} endpoint(s)/slot(s) forgotten - {why}")


def _forget_ep(hv: dict, e) -> None:
    """Drop everything the relay knows about one joiner endpoint in one host flow; its next packet is a new link."""
    hv["eps"].pop(e, None)
    for k in ("src", "slot", "ep_rx", "hports", "src_logged", "first_seen", "leaving", "conn", "silence"):
        hv.get(k, {}).pop(e, None)
    hv.get("probe", set()).discard(e)
    hv.get("returned", set()).discard(e)
    hv.get("known", set()).discard(e)
    for k in [k for k in hv.get("held", {}) if k[0] == e]:
        hv["held"].pop(k, None)


def leave_expire(now: float) -> None:
    """Forget links kept by the leave grace once they have been quiet LEAVE_IDLE_S."""
    for h, hv in RELAY_HOSTS.items():
        lv = hv.get("leaving")
        if not lv:
            continue
        for e in list(lv):
            last = hv["eps"].get(e, (None, -1e9))[1]
            if now - last >= LEAVE_IDLE_S:
                hole = hv.get("src", {}).get(e)
                _forget_ep(hv, e)
                log(f"RELAY host {h}: {_who(e[0])}:{e[1]} quiet {now - last:.1f}s after the leave; hole {hole} released")


def forget_idle_links(j: str, now: float, why: str) -> None:
    """At a JOIN_ROOM: forget every link of joiner IP j that is not sending right now (see JOIN_IDLE_S)."""
    for h, hv in RELAY_HOSTS.items():
        gone = [e for e in hv["eps"] if e[0] == j and now - hv["eps"][e][1] > JOIN_IDLE_S]
        for e in gone:
            _forget_ep(hv, e)
        if gone:
            log(f"RELAY host {h}: {why}: {len(gone)} idle link(s) of {_who(j)} forgotten "
                f"({', '.join(str(e[1]) for e in gone)}), so the join starts on one fresh hole")


def relay_from_joiner(sock: socket.socket, data: bytes, addr: tuple[str, int], now: float) -> None:
    """Datagram from an outside joiner on a relay port -> host:3658 from 45000."""
    ent = RELAY_MAP.get(addr[0])
    if ent is None:
        guard_unmapped(addr[0], len(data))
        if addr[0] not in RELAY_UNMAPPED_LOGGED:
            RELAY_UNMAPPED_LOGGED.add(addr[0])
            log(f"RELAY drop: {len(data)}B on :{sock.getsockname()[1]} from {addr[0]}:{addr[1]} - no mapping from the lobby server (logged once)")
        return
    host = ent["host"]
    hv = RELAY_HOSTS.get(host)
    if hv is None:
        hv = {"joiner": addr, "sock": sock, "rx": 0, "tx": 0, "last": now, "first": now, "eps": {}, "dump_rx": 0, "dump_tx": 0}
        RELAY_HOSTS[host] = hv
        log(f"RELAY host {host}: flow opened by {addr[0]}:{addr[1]} via relay port {sock.getsockname()[1]} -> {host}:{RELAY_HOST_PORT} from {NAT_MAIN_SOCK.getsockname()[1]}")
    elif hv["joiner"] != addr or hv["sock"] is not sock:
        if hv["joiner"][0] != addr[0]:
            # With several joiners on one host this fires on almost every packet (88% of the log on 2026-11-06): once a minute is enough.
            if now - _IPCHG_LOGGED.get((host, addr[0]), -1e9) >= IPCHG_LOG_S:
                _IPCHG_LOGGED[(host, addr[0])] = now
                log(f"RELAY host {host}: joiner IP changes {hv['joiner'][0]} -> {addr[0]} (v1: one joiner per host, replies follow the last sender)")
        elif addr not in hv["eps"]:
            log(f"RELAY host {host}: joiner {addr[0]} now also sends from port {addr[1]} (replies follow the last sender)")
        hv["joiner"] = addr
        hv["sock"] = sock
    prev_t = hv["eps"][addr][1] if addr in hv["eps"] else None
    if prev_t is not None:
        hv.setdefault("known", set()).add(addr)        # seen before this packet: not a fresh port (OWN_SOCKET_KEEP)
    hv["eps"][addr] = (sock, now)
    # one "connection" of this endpoint = a run of packets without a gap > LINK_IDLE_S (used by the no-reply move)
    conns = hv.setdefault("conn", {})
    if addr not in conns or prev_t is None or now - prev_t > LINK_IDLE_S:
        conns[addr] = {"start": now, "n": 0, "moves": 0}
        if prev_t is not None and now - prev_t >= OWN_RETURN_S:
            hv.setdefault("returned", set()).add(addr)     # this socket comes back after a long silence (OWN_SOCKET_KEEP)
        if AGE_LOG and RELAY_HOLES:
            age_start(hv, host, addr, now, prev_t)
    conns[addr]["n"] += 1
    hv["rx"] += 1
    hv["last"] = now
    # host destination port for this joiner endpoint: first endpoint -> 3658, later ones -> alt host sockets (if enabled)
    hports = hv.setdefault("hports", {})
    hport = RELAY_HOST_PORT
    if RELAY_ALT_HOST_PORTS and not RELAY_HOLES:
        slot = pick_slot(hv, addr, now, prev_t, 1 + len(RELAY_ALT_HOST_PORTS))
        nominal = ([RELAY_HOST_PORT] + RELAY_ALT_HOST_PORTS)[slot]
        hport = SOCKET_EXT.get(host, {}).get(str(slot + 1)) or nominal
        if hports.get(addr) != hport:
            hports[addr] = hport
            log(f"RELAY host {host}: joiner link {addr[0]}:{addr[1]} -> host socket {slot + 1} (port {nominal}, external {hport}) from 45000")
    if RELAY_DUMP and hv["dump_rx"] < RELAY_DUMP:
        hv["dump_rx"] += 1
        log(f"RELAY DUMP J->H {addr[0]}:{addr[1]} -> :{hport} {len(data)}B: {data[:64].hex(' ')}")
    src_sock, src_port = NAT_MAIN_SOCK, NAT_MAIN_SOCK.getsockname()[1]
    if RELAY_HOLES:
        hport = RELAY_HOST_PORT
        src_port = pick_hole_port(hv, addr, now, prev_t, host)
        src_sock = HOLE_SOCKS[src_port]
        if AGE_PENDING:
            age_hole(host, addr, src_port, now)
            if SHADOW and (host, addr) in AGE_PENDING:
                shadow_link(hv, AGE_PENDING[(host, addr)], now)
    hv.setdefault("ep_rx", {})[addr] = hv.setdefault("ep_rx", {}).get(addr, 0) + 1
    if RELAY_HOLES and HOLD_MODE != "off":
        held = hv.setdefault("held", {})
        key = (addr, src_port)
        if hole_cold(host, src_port, now):
            st = hold_state(host)
            if st == "strict":
                if key not in held:
                    held[key] = [now, 0]
                    last = HOST_TRAFFIC.get(host, {}).get(src_port)
                    log(f"RELAY host {host}: holding {_who(addr[0])}:{addr[1]} on hole {src_port} until the host's ping opens it "
                        f"(strict NAT; host last sent there {f'{now - last:.0f}s ago' if last else 'never'})")
                    age_note(host, addr, now, f"held on {src_port}")
                held[key][1] += 1
                js = hv.setdefault("joiner_since", {})
                if src_port not in js or HOST_TRAFFIC.get(host, {}).get(src_port, 0.0) > js[src_port]:
                    js[src_port] = now   # lets hole_steering see a link waiting on a never-opened / dead hole
                return
            if st == "unknown":
                pr = HOST_HOLD.setdefault(host, {"state": "unknown"}).setdefault("probe", {})
                if OPEN_STRICT:
                    # The probe entry itself is dropped and started again on a hole the host has used before
                    # (hold_detect). Remember this sending run's first packet so a late answer cannot look fast.
                    link0 = HOST_HOLD[host].setdefault("link0", {})
                    slot = link0.get(src_port)
                    if slot is None or slot[2] != addr or now - slot[1] > OPEN_RUN_GAP_S:
                        link0[src_port] = [now, now, addr]
                    else:
                        slot[1] = now
                pr.setdefault(src_port, [now, 0, addr])[1] += 1
            RELAY_COLD_SEND.setdefault(host, {})[src_port] = now
        elif key in held:
            t0, n = held.pop(key)
            log(f"RELAY host {host}: hole {src_port} opened by the host after {now - t0:.0f}s; {_who(addr[0])}:{addr[1]} "
                f"goes through ({n} packets were held)")
            age_note(host, addr, now, f"released on {src_port} by the host's ping")
    if RELAY_HOLES:
        ext = HOST_EXT.get(host, {})
        hport = ext.get(src_port) or (next(iter(ext.values())) if ext else RELAY_HOST_PORT)
        if hv.get("dst_logged", {}).get(src_port) != hport:
            hv.setdefault("dst_logged", {})[src_port] = hport
            log(f"RELAY host {host}: hole {src_port} -> external port {hport}" + ("" if src_port in ext else " (not yet observed on this hole; guessing)"))
    try:
        src_sock.sendto(data, (host, hport))
        guard_count(("J", addr, src_port), f"{_who(addr[0])}:{addr[1]} -> host {host} (hole {src_port})", len(data), now)
        if RELAY_HOLES:
            RELAY_TRAFFIC.setdefault(host, {})[src_port] = now
            pool_sent(host, src_port, now)
            if SHADOW:
                shadow_sent(host, src_port, addr, now)
            js = hv.setdefault("joiner_since", {})
            if src_port not in js or HOST_TRAFFIC.get(host, {}).get(src_port, 0.0) > js[src_port]:
                js[src_port] = now
    except OSError as e:
        log(f"RELAY -> host {host} send failed: {e}")
        return
    if hv["rx"] in (1, 2, 5, 10, 50) or hv["rx"] % 500 == 0:
        log(f"RELAY J->H {addr[0]}:{addr[1]} -> {host}:{hport} {len(data)}B (#{hv['rx']})")


def relay_from_host(data: bytes, addr: tuple[str, int], now: float, nat_port: int = 45000) -> bool:
    """Datagram from a relayed host's game socket (on 45000 or a hole port) -> its joiner. Returns True if consumed."""
    hv = RELAY_HOSTS.get(addr[0])
    if hv is None:
        return False
    hv["tx"] += 1
    hv["last"] = now
    dest, dsock = hv["joiner"], hv["sock"]
    cands = []
    if RELAY_ALT_HOST_PORTS and not RELAY_HOLES:
        SOCKET_EXT.setdefault(addr[0], {})   # replies come from the host socket the link was delivered to
    if RELAY_HOLES and nat_port in RELAY_HOLES:
        if SHADOW:
            shadow_reply(addr[0], nat_port, now)
        note_host_reply(addr[0], nat_port, now)
        HOST_TRAFFIC.setdefault(addr[0], {})[nat_port] = now
        hd = HOST_DATA.setdefault(addr[0], {})
        if now - max(hd.values(), default=-1e9) > 2.0:
            hv["host_active_since"] = now    # the host is sending game data again (after a map load or a lull)
        hd[nat_port] = now
        ext = HOST_EXT.setdefault(addr[0], {})
        if ext.get(nat_port) != addr[1]:
            log(f"RELAY host {addr[0]}: game data on hole {nat_port} from external port {addr[1]}" + (f" (was {ext[nat_port]})" if nat_port in ext else ""))
            ext[nat_port] = addr[1]
        HOST_EXT_LOCK.setdefault(addr[0], set()).add(nat_port)
        # multi-hole: the hole port the reply arrived on identifies the joiner link
        owners = [e for e, p in hv.get("src", {}).items() if p == nat_port and e in hv["eps"]]
        if owners:
            best = max(owners, key=lambda e: hv["eps"][e][1])
            dest, dsock = best, hv["eps"][best][0]
    else:
        # Route by the host's source socket: the joiner endpoints mapped to that host port are the candidates; with one
        # candidate it is unambiguous, with several (alt ports off: all on 3658) the most recent sender among them wins.
        cands = [ep for ep, hp in hv.get("hports", {}).items() if hp == addr[1] and ep in hv["eps"]]
    if cands:
        if hv["joiner"] in cands:
            dest, dsock = hv["joiner"], hv["sock"]
        else:
            best = max(cands, key=lambda ep: hv["eps"][ep][1])
            dest, dsock = best, hv["eps"][best][0]
    if RELAY_DUMP and hv["dump_tx"] < RELAY_DUMP:
        hv["dump_tx"] += 1
        log(f"RELAY DUMP H->J {addr[0]}:{addr[1]} -> {dest[0]}:{dest[1]} {len(data)}B: {data[:64].hex(' ')}")
    try:
        dsock.sendto(data, dest)
        guard_count(("H", addr[0], nat_port, dest), f"host {_who(addr[0])} (hole {nat_port}) -> {dest[0]}:{dest[1]}", len(data), now)
        hv.setdefault("ep_tx", {})[dest] = hv.setdefault("ep_tx", {}).get(dest, 0) + 1
        if AGE_PENDING and RELAY_HOLES and nat_port in RELAY_HOLES:
            rec = AGE_PENDING.pop((addr[0], dest), None)
            if rec is not None:
                age_emit(rec, now, "answered", via=nat_port)
    except OSError as e:
        log(f"RELAY -> joiner {hv['joiner']} send failed: {e}")
        return True
    if RELAY_FANOUT_S > 0:
        for ep, (esock, et) in list(hv["eps"].items()):
            if ep == hv["joiner"] or ep[0] != hv["joiner"][0]:
                continue
            if now - et > RELAY_FANOUT_S:
                hv["eps"].pop(ep, None)
                continue
            try:
                esock.sendto(data, ep)
            except OSError:
                pass
    if hv["tx"] in (1, 2, 5, 10, 50) or hv["tx"] % 500 == 0:
        log(f"RELAY H->J {addr[0]}:{addr[1]} -> {dest[0]}:{dest[1]} {len(data)}B (#{hv['tx']}) from :{dsock.getsockname()[1]}")
    return True


def _who(ip: str) -> str:
    u = (RELAY_MAP.get(ip) or {}).get("user")
    return f"{u} {ip}" if u else ip


# ---- Failure-step lines (see FAIL_LOG). Read-only: these functions only read relay state and write log lines. ----
_FAIL_ERR = []


def _fail_safe(fn):
    def run(*a, **k):
        if not FAIL_LOG:
            return None
        try:
            return fn(*a, **k)
        except Exception as e:      # noqa: BLE001 - log-only feature
            if not _FAIL_ERR:
                _FAIL_ERR.append(e)
                log(f"FAIL log error (ignored, relaying unaffected): {e!r}")
    run.__name__ = fn.__name__
    return run


def _nat_who(clt) -> str:
    n = NAT_NAMES.get(clt.addr)
    return f"{n + ' ' if n else ''}{clt.addr}:{clt.port}"


@_fail_safe
def nat_step_waiting(clt, now: float, why: str) -> None:
    """A NAT session ends (FIN or expiry): name the sockets that were still waiting for their ping."""
    wait = [(r["sock"], now - r["t"]) for r in (getattr(clt, "reqs", None) or {}).values()
            if r.get("pending") and not r.get("extra") and r.get("t") is not None]
    if wait:
        log(f"NAT step: {_nat_who(clt)} {why} while socket(s) " + ", ".join(f"{s} (PORT_ID sent {a:.0f}s ago)" for s, a in wait)
            + " still had not pinged 45000: the console's NAT test for them stopped at 'PORT_ID sent'")
    players = _hosted_players(clt.addr, now)
    if players and "SYN" not in why:
        NAT_LOST_HOST[clt.addr] = now
        log(f"NAT step: HOST {_nat_who(clt)} {why} while hosting {len(players)} player(s) ({', '.join(players)}). "
            f"The relay can no longer steer its pings, so no new hole can be opened for it: a player who comes back on a "
            f"hole that is not already open may wait until the game drops him. It needs to log in again. (2026-11-17 16:15, "
            f"COOLDOR: its console stopped acknowledging steers, then closed the session - router hardware acceleration was off)")


NAT_LOST_HOST: dict = {}   # host ip -> when its NAT session ended while it was hosting (log only)
_ORPHAN_LOGGED: dict = {}


def _hosted_players(host: str, now: float = None) -> list:
    """Players mapped to this host - only while the host is actually relaying game data (within the last 60 s), so a
    normal logout after the room has closed is not reported."""
    if now is not None and now - max(HOST_DATA.get(host, {}).values(), default=-1e9) > 60:
        return []
    return sorted(f"{NAT_NAMES.get(j) or j}" for j, m in RELAY_MAP.items() if isinstance(m, dict) and m.get("host") == host)


@_fail_safe
def nat_step_orphan_ping(addr, now: float) -> None:
    """A console that is hosting pings with an id the relay no longer knows: its NAT session is gone. Once per host per 10 min."""
    host = addr[0]
    players = _hosted_players(host, now)
    if not players or now - _ORPHAN_LOGGED.get(host, -1e9) < 600:
        return
    _ORPHAN_LOGGED[host] = now
    lost = NAT_LOST_HOST.get(host)
    log(f"NAT step: HOST {NAT_NAMES.get(host) + ' ' if NAT_NAMES.get(host) else ''}{host} is hosting "
        f"{len(players)} player(s) but pings without a NAT session" + (f" (it closed it {now - lost:.0f}s ago)" if lost else "")
        + ": its pings cannot be steered, so no new hole can be opened for it until it logs in again")


@_fail_safe
def nat_step_ping(clt, r: dict, now: float) -> None:
    if r.get("stalled") and r.get("t") is not None:
        log(f"NAT step: {_nat_who(clt)} socket {r['sock']} pinged {now - r['t']:.0f}s after its PORT_ID (late; reported "
            f"stalled earlier), ADDRESS follows")


@_fail_safe
def nat_step_tick(now: float) -> None:
    for clt in CLIENTS:
        for r in (getattr(clt, "reqs", None) or {}).values():
            if (r.get("pending") and not r.get("extra") and not r.get("stalled") and r.get("t") is not None
                    and now - r["t"] >= NAT_STALL_S):
                r["stalled"] = True
                done = sorted(str(o["sock"]) for o in clt.reqs.values() if not o.get("pending") and not o.get("extra"))
                log(f"NAT step: {_nat_who(clt)} socket {r['sock']} stopped at 'PORT_ID sent': no ping to 45000 in "
                    f"{now - r['t']:.0f}s (sockets done: {', '.join(done) or 'none'}). The console's NAT test is stuck here; "
                    f"if it never pings, this console cannot be reached on that socket")


def _probe_txt(host: str, now: float) -> str:
    pr = SHADOW_EV.get(host, {}).get("probe")
    if not pr or pr.get("t") is None:
        return "router probe: none this run"
    return (f"router probe {now - pr['t']:.0f}s ago: fresh {pr.get('last_fresh_ok', 0)}/2 seen; all runs fresh "
            f"{pr['fresh_ok']} seen / {pr['fresh_miss']} not, stale {pr['stale_ok']} seen / {pr['stale_miss']} not")


@_fail_safe
def fail_why(hv: dict, host: str, old: int, c: dict, group, now: float) -> None:
    """A no-reply move: what the relay knew about the dead hole, and what the router probe said."""
    start = c["start"]
    tr = HOST_TRAFFIC.get(host, {}).get(old)
    hd = HOST_DATA.get(host, {})
    data = hd.get(old)
    before = None if tr is None or tr > start else start - tr
    others = sorted(((now - t, p) for p, t in hd.items() if p != old and now - t <= NOREPLY_HOST_ALIVE_S))[:3]
    if before is not None and before <= HOLD_PING_OPEN_S:
        what = (f"the host used this hole {before:.0f}s before the link started and its router still dropped every packet: "
                f"the router, not a cold hole")
    elif tr is None:
        what = "the host had never used this hole (cold, as expected on a strict NAT)"
    else:
        what = (f"the host last used this hole {start - tr:.0f}s before the link started (cold)" if tr <= start else
                "the host used this hole only after the link started (its ping or data, no answer to the link)")
    log(f"FAIL why: host {_hname(host)} hole {old}: no reply to {', '.join(f'{_who(e[0])}:{e[1]}' for e in group)} for "
        f"{now - start:.0f}s ({c['n']} packets). Host traffic there (ping or data) "
        f"{'never' if tr is None else f'{now - tr:.0f}s ago'}, game data there {'never' if data is None else f'{now - data:.0f}s ago'}; "
        f"host answering " + (", ".join(f"{p} {a:.0f}s ago" for a, p in others) or "no other hole") + f" -> {what} | "
        + _probe_txt(host, now))


def _bad_txt(host: str, now: float) -> str:
    return ",".join(f"{p}:{(BAD_HOLE_S - (now - t)) / 60:.0f}m-left" for p, t in sorted(BAD_HOLES.get(host, {}).items())
                    if now - t < BAD_HOLE_S)


# ---- Fast resume flag (SCCT_RELAY_FAST_RESUME_LOG, default OFF; LOG ONLY: no packet is handled differently) ----
# 2026-11-19 00:36 (STRIDERSTROKE host, SNAKE) and 23:46 (SNAKE host, IY0): at a round end each player's console wakes a link
# that was quiet for 10+ min and the host answers it ~8-12 s later, after loading the next map. In 127 of 129 such returns
# nobody dropped. In the other two the host answered within 0.1 s, before its map load; it then went quiet for the load
# and ~6.5 s into that silence the player's console left the room (GROUP_LEAVE) and had to rejoin. These lines flag every
# such fast answer and say whether that player left the room in the next FAST_WATCH_S seconds:
#   FAST RESUME: <player> back after Ns quiet, answered by the host in 0.0s (before its map load) - watching 20 s
#   FAST RESUME follow-up: <player> left the room 8.8s after the fast answer (GROUP_LEAVE ...) | stayed in the room
FAST_RESUME_LOG = _env_on("SCCT_RELAY_FAST_RESUME_LOG", "0")
FAST_QUIET_S = float(os.environ.get("SCCT_RELAY_FAST_QUIET_S", "300"))   # a link quiet this long woke at a round end
FAST_WAIT_S = float(os.environ.get("SCCT_RELAY_FAST_WAIT_S", "1.0"))     # answered faster than this counts as fast
FAST_WATCH_S = float(os.environ.get("SCCT_RELAY_FAST_WATCH_S", "20"))
FAST_WATCH = {}        # joiner IP -> (t of the answer, endpoint, host, wait, quiet)
FAST_STATS = {"fast": 0, "left": 0, "stayed": 0}
_FAST_ERR = []


def _fast_safe(fn):
    def run(*a, **k):
        if not FAST_RESUME_LOG or _FAST_ERR:
            return None
        try:
            return fn(*a, **k)
        except Exception as e:  # logging must never affect routing
            _FAST_ERR.append(e)
            log(f"FAST RESUME error (logging off from here): {e!r}")
            return None
    run.__name__ = fn.__name__
    return run


@_fast_safe
def fast_resume_check(rec: dict, now: float, outcome: str) -> None:
    """Called with every finished AGE record: flag a long-quiet in-game link that the host answered almost at once."""
    if outcome != "answered" or rec.get("kind") != "resume" or rec.get("ctx") != "in-game":
        return
    quiet, wait = rec.get("quiet") or 0.0, now - rec["t0"]
    if quiet < FAST_QUIET_S or wait >= FAST_WAIT_S:
        return
    ep, host = rec["ep"], rec["host"]
    FAST_STATS["fast"] += 1
    FAST_WATCH[ep[0]] = (now, ep, host, wait, quiet)
    hname = NAT_NAMES.get(host)
    log(f"FAST RESUME: {_who(ep[0])}:{ep[1]} back after {quiet:.0f}s quiet, answered by host {hname + ' ' if hname else ''}{host} "
        f"in {wait:.1f}s (before its map load; usually 8-12 s) - watching {FAST_WATCH_S:.0f}s for a room leave (log only)")


@_fast_safe
def fast_resume_leave(j: str, why: str, now: float) -> None:
    w = FAST_WATCH.get(j)
    if w is None or not why.startswith("GROUP_LEAVE") or now - w[0] > FAST_WATCH_S:
        return                      # other resets (a new join) keep watching; fast_resume_tick closes stale ones
    FAST_WATCH.pop(j, None)
    FAST_STATS["left"] += 1
    log(f"FAST RESUME follow-up: {_who(j)}:{w[1][1]} LEFT the room {now - w[0]:.1f}s after the fast answer ({why}); "
        f"this run: fast answers {FAST_STATS['fast']}, followed by a room leave {FAST_STATS['left']}, stayed {FAST_STATS['stayed']} (log only)")


@_fast_safe
def fast_resume_tick(now: float) -> None:
    for j, w in list(FAST_WATCH.items()):
        if now - w[0] > FAST_WATCH_S:
            FAST_WATCH.pop(j, None)
            FAST_STATS["stayed"] += 1
            log(f"FAST RESUME follow-up: {_who(j)}:{w[1][1]} stayed in the room {FAST_WATCH_S:.0f}s after the fast answer; "
                f"this run: fast answers {FAST_STATS['fast']}, followed by a room leave {FAST_STATS['left']}, stayed {FAST_STATS['stayed']} (log only)")


# ---- AGE log (see AGE_LOG). Read-only: these functions only read relay state and write log lines. ----
_AGE_ERR = []


def _age_safe(fn):
    """The AGE hooks run in the packet path: an error in them must never stop a packet. Logged once, then ignored."""
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:      # noqa: BLE001 - log-only feature
            if not _AGE_ERR:
                _AGE_ERR.append(e)
                log(f"AGE log error (ignored, relaying unaffected): {e!r}")
    run.__name__ = fn.__name__
    return run


def _ago(t, t0: float) -> str:
    return "never" if t is None else f"{t0 - t:.0f}s"


@_age_safe
def age_start(hv: dict, host: str, ep, now: float, prev_t) -> None:
    """A link starts, or comes back after a gap, on its hole: remember what the relay knew about that hole right now."""
    key = (host, ep)
    old = AGE_PENDING.pop(key, None)
    if old is not None:
        age_emit(old, now, "never", why=f"link paused {now - prev_t:.0f}s before any host packet" if prev_t else "restarted")
    hd = HOST_DATA.get(host, {})
    last_any_data = max(hd.values(), default=None)
    ent = RELAY_MAP.get(ep[0]) or {}
    if ent.get("t") is not None and now - ent["t"] <= HOLD_JOIN_WINDOW_S:
        ctx = "join"
    elif last_any_data is None or now - last_any_data > 2.0:
        ctx = "host-loading"           # the host is sending game data on no hole right now (map load or menu)
    elif now - hv.get("host_active_since", -1e9) <= 15.0:
        ctx = "after-load"             # the host came back from such a silence in the last 15 s
    else:
        ctx = "in-game"
    rec = {"t0": now, "host": host, "ep": ep, "kind": "new" if prev_t is None else "resume",
           "quiet": None if prev_t is None else now - prev_t, "state": hold_state(host), "ctx": ctx,
           "hole": None, "cur": None, "events": [], "bad": _bad_txt(host, now) if FAIL_LOG else ""}
    hole = hv.get("src", {}).get(ep)
    if hole is not None:
        _age_fill(rec, host, hole, now)
    AGE_PENDING[key] = rec


def _age_fill(rec: dict, host: str, hole: int, now: float) -> None:
    rec["hole"] = rec["cur"] = hole
    rec["data"] = HOST_DATA.get(host, {}).get(hole)
    rec["any"] = HOST_TRAFFIC.get(host, {}).get(hole)
    rec["cold"] = hole_cold(host, hole, now)


@_age_safe
def age_hole(host: str, ep, hole: int, now: float) -> None:
    """After the hole choice for a packet: a new link learns its hole; a link put on another hole is noted."""
    rec = AGE_PENDING.get((host, ep))
    if rec is None:
        return
    if rec["hole"] is None:
        _age_fill(rec, host, hole, now)
    elif rec["cur"] != hole:
        rec["events"].append((now - rec["t0"], f"on {hole} (was {rec['cur']})"))
        rec["cur"] = hole


@_age_safe
def age_note(host: str, ep, now: float, what: str, hole=None) -> None:
    rec = AGE_PENDING.get((host, ep))
    if rec is None:
        return
    rec["events"].append((now - rec["t0"], what))
    if hole is not None:
        rec["cur"] = hole


@_age_safe
def age_emit(rec: dict, now: float, outcome: str, via=None, why: str = "") -> None:
    hname = NAT_NAMES.get(rec["host"])
    parts = [f"AGE host={hname + ' ' if hname else ''}{rec['host']} ({rec['state']})",
             f"player={_who(rec['ep'][0])}:{rec['ep'][1]}", f"kind={rec['kind']}"]
    if rec["quiet"] is not None:
        parts.append(f"quiet={rec['quiet']:.0f}s")
    if rec["hole"] is None:
        parts.append("hole=none")
    else:
        parts += [f"hole={rec['hole']}", f"host_data={_ago(rec['data'], rec['t0'])}", f"host_any={_ago(rec['any'], rec['t0'])}",
                  f"cold={'yes' if rec['cold'] else 'no'}"]
    parts += [f"ctx={rec['ctx']}", f"outcome={outcome}"]
    parts.append(f"wait={now - rec['t0']:.1f}s" if outcome == "answered" else f"after={now - rec['t0']:.0f}s")
    if via is not None and via != rec["hole"]:
        parts.append(f"via={via}")
    if rec.get("bad"):
        parts.append(f"avoided={rec['bad']}")
    if rec["events"]:
        parts.append("steps=[" + "; ".join(f"{t:.0f}s {m}" for t, m in rec["events"]) + "]")
    if why:
        parts.append(f"({why})")
    if (SLOW_JOIN_LOG and rec["kind"] == "new" and outcome == "answered"
            and now - rec["t0"] > GAME_JOIN_LIMIT_S and rec.get("slow_warned")):
        parts.append(f"(SLOW JOIN: first host packet after the game's {GAME_JOIN_LIMIT_S:.0f}s join limit)")
    log(" ".join(parts))
    if FAST_RESUME_LOG:
        fast_resume_check(rec, now, outcome)


@_age_safe
def age_tick(now: float) -> None:
    """Close attempts that never got a host packet: after AGE_TIMEOUT_S, or once the relay forgot the link."""
    if not AGE_PENDING:
        return
    for key, rec in list(AGE_PENDING.items()):
        host, ep = key
        hv = RELAY_HOSTS.get(host)
        if hv is None or ep not in hv.get("eps", {}):
            AGE_PENDING.pop(key, None)
            age_emit(rec, now, "never", why="link forgotten by the relay (leave, join reset or host gone)")
        elif now - rec["t0"] >= AGE_TIMEOUT_S:
            AGE_PENDING.pop(key, None)
            last = hv["eps"][ep][1]
            age_emit(rec, now, "never", why=f"no host packet in {AGE_TIMEOUT_S:.0f}s; player "
                                            + ("still sending" if now - last <= LINK_IDLE_S else f"quiet {now - last:.0f}s"))
        elif (SLOW_JOIN_LOG and rec["kind"] == "new" and not rec.get("slow_warned")
              and now - rec["t0"] >= SLOW_JOIN_WARN_S and now - hv["eps"][ep][1] <= LINK_IDLE_S):
            # only while the player still sends on it: a one-second join probe that stopped long ago is not a wait
            rec["slow_warned"] = True
            hname = NAT_NAMES.get(host)
            log(f"SLOW JOIN host={hname + ' ' if hname else ''}{host} player={_who(ep[0])}:{ep[1]} "
                f"hole={rec['cur'] if rec['cur'] is not None else 'none'} ctx={rec['ctx']}: no host packet after "
                f"{now - rec['t0']:.0f}s; the game gives up on a join at {GAME_JOIN_LIMIT_S:.0f}s (InitialConnectTimeout)"
                + (f"; steps=[" + "; ".join(f"{t:.0f}s {m}" for t, m in rec["events"]) + "]" if rec["events"] else ""))



# ---- Shadow hole classes (see SHADOW). Read-only: these functions only read relay state and write log lines. ----
_SHADOW_ERR = []


def _shadow_safe(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:      # noqa: BLE001 - log-only feature
            if not _SHADOW_ERR:
                _SHADOW_ERR.append(e)
                log(f"SHADOW error (ignored, relaying unaffected): {e!r}")
    run.__name__ = fn.__name__
    return run


def hole_class(host: str, port: int, now: float):
    last = HOST_TRAFFIC.get(host, {}).get(port)
    if last is None:
        return "fresh", None
    idle = now - last
    if idle <= HOLD_PING_OPEN_S:
        return "pinged", idle
    if pool_alive(host, port, now):
        return "kept", idle          # a spare the pool holds open with keepalives: trusted today, not a forward test
    if idle >= SHADOW_FRESH_S:
        return "fresh", idle
    return "stale", idle


def _shadow_ev(host: str) -> dict:
    return SHADOW_EV.setdefault(host, {"fresh": [0, 0, 0], "stale": [0, 0, 0], "would": 0, "links": 0})


def _cls_txt(cls: str, idle) -> str:
    return f"{cls} (host never sent there)" if idle is None else f"{cls} (host silent there {idle:.0f}s)"


def _hname(host: str) -> str:
    n = NAT_NAMES.get(host)
    return f"{n} {host}" if n else host


@_shadow_safe
def shadow_sent(host: str, port: int, ep, now: float) -> None:
    """A joiner packet was just sent into hole `port`: start (or feed) a test if that hole is fresh or stale."""
    key = (host, port)
    p = SHADOW_PROBE.get(key)
    if p is not None:
        p["n"] += 1
        p["last"] = now
        return
    if now - SHADOW_DONE.get(key, -1e9) < 60.0:
        return
    cls, idle = hole_class(host, port, now)
    if cls in ("pinged", "kept"):
        return
    rec = AGE_PENDING.get((host, ep)) or {}
    SHADOW_PROBE[key] = {"t0": now, "n": 1, "last": now, "cls": cls, "idle": idle, "ep": ep,
                         "state": hold_state(host), "ctx": rec.get("ctx", "?")}


def _shadow_end(host: str, port: int, p: dict, now: float, kind, text: str) -> None:
    SHADOW_PROBE.pop((host, port), None)
    SHADOW_DONE[(host, port)] = now
    if kind is not None:
        _shadow_ev(host)[p["cls"]][kind] += 1
    ev = _shadow_ev(host)
    log(f"SHADOW test host={_hname(host)} ({p['state']}) hole={port} {_cls_txt(p['cls'], p['idle'])} "
        f"player={_who(p['ep'][0])}:{p['ep'][1]} ctx={p['ctx']}: {text} | fresh ok/no-reply/ambiguous "
        f"{'/'.join(map(str, ev['fresh']))}, stale {'/'.join(map(str, ev['stale']))}")


@_shadow_safe
def shadow_reply(host: str, port: int, now: float) -> None:
    """Game data from the host on hole `port` (call BEFORE HOST_TRAFFIC / HOST_DATA are updated for this packet)."""
    p = SHADOW_PROBE.get((host, port))
    if p is None:
        return
    if HOST_TRAFFIC.get(host, {}).get(port, -1e9) >= p["t0"]:
        _shadow_end(host, port, p, now, None, "the host pinged it during the test: no evidence")
        return
    hd = HOST_DATA.get(host, {})
    last_any = max(hd.values(), default=None)
    back = (RELAY_HOSTS.get(host) or {}).get("host_active_since", -1e9)
    if last_any is None or now - last_any > 2.0 or (back > p["t0"] and now - back <= HOLD_RESUME_S):
        _shadow_end(host, port, p, now, 2, f"answered {now - p['t0']:.1f}s after the first packet, but in the host's first "
                                           f"burst after a silence (map load): ambiguous")
        return
    _shadow_end(host, port, p, now, 0, f"answered {now - p['t0']:.1f}s after the first packet ({p['n']} sent), host busy "
                                       f"with others: OK - packets reached it without a ping")


@_shadow_safe
def shadow_tick(now: float) -> None:
    for (host, port), p in list(SHADOW_PROBE.items()):
        if HOST_TRAFFIC.get(host, {}).get(port, -1e9) >= p["t0"]:
            _shadow_end(host, port, p, now, None, "the host pinged it during the test: no evidence")
            continue
        if now - p["last"] > LINK_IDLE_S or now - p["t0"] > 30.0:
            if now - p["t0"] >= SHADOW_TEST_S:
                _shadow_end(host, port, p, now, None, f"no verdict ({p['n']} sent; player stopped or host not sending to "
                                                      f"others long enough): no evidence")
            else:
                SHADOW_PROBE.pop((host, port), None)
            continue
        hv = RELAY_HOSTS.get(host) or {}
        start = max(p["t0"], hv.get("host_active_since", -1e9))
        if now - start < SHADOW_TEST_S or p["n"] < SHADOW_TEST_PKTS:
            continue
        hd = HOST_DATA.get(host, {})
        alive = sorted(q for q, t in hd.items() if q != port and now - t <= 1.0)
        if not alive:
            continue                 # host loading a map: no verdict yet
        _shadow_end(host, port, p, now, 1, f"NO REPLY for {now - p['t0']:.1f}s ({p['n']} sent) while the host sent game "
                                           f"data on {', '.join(map(str, alive))}")
    for host, ev in SHADOW_EV.items():
        if now - SHADOW_SUM_T.setdefault(host, now) >= SHADOW_SUMMARY_S:
            SHADOW_SUM_T[host] = now
            hint = SHADOW_HINT.get(host)
            log(f"SHADOW summary host={_hname(host)} ({hold_state(host)}): fresh ok/no-reply/ambiguous "
                f"{'/'.join(map(str, ev['fresh']))}, stale {'/'.join(map(str, ev['stale']))}; links on fresh/stale holes "
                f"{ev['links']}, would-moves {ev['would']}; UPnP hint " + (f"{now - hint:.0f}s ago" if hint else "none")
                + (f"; router probe fresh {ev['probe']['fresh_ok']}/{ev['probe']['fresh_ok'] + ev['probe']['fresh_miss']} seen, "
                   f"stale {ev['probe']['stale_ok']}/{ev['probe']['stale_ok'] + ev['probe']['stale_miss']} seen"
                   if ev.get("probe") else ""))


@_shadow_safe
def shadow_link(hv: dict, rec: dict, now: float) -> None:
    """A link starts or returns on a hole (from the AGE record): if that hole is fresh or stale, say what a fresh-hole rule
    would do. Nothing is changed."""
    if rec.get("shadowed") or rec.get("hole") is None:
        return
    rec["shadowed"] = True
    host, ep, hole = rec["host"], rec["ep"], rec["hole"]
    cls, idle = hole_class(host, hole, rec["t0"])
    if cls in ("pinged", "kept"):
        return
    ev = _shadow_ev(host)
    ev["links"] += 1
    live = {hv.get("src", {}).get(e) for e, (_, t) in hv.get("eps", {}).items() if e != ep and now - t <= LINK_IDLE_S}
    claimed = {h for h, t in ev.setdefault("claim", {}).items() if now - t <= 30.0}   # offered to another link just now
    free = [h for h in RELAY_HOLES if h != hole and h not in live and h not in claimed and hole_class(host, h, now)[0] == "fresh"]
    ok, bad, amb = ev["fresh"]
    pr = ev.get("probe") or {}
    by_probe = (pr.get("last_fresh_ok", 0) == 2 and pr.get("t") is not None and now - pr["t"] <= 3600.0)
    if rec["state"] == "open":
        do = "host judged open: today's routing already sends at once"
    elif rec["ctx"] == "join":
        do = "a join: today's routing (never changed during a join)"
    elif bad > 0 or not (ok >= SHADOW_PROVEN or by_probe):
        do = (f"fresh holes unproven for this host (ok {ok}, no-reply {bad}, ambiguous {amb}; router probe "
              + (f"last run {pr.get('last_fresh_ok', 0)}/2 fresh seen" if pr else "none") + "): today's routing")
    elif cls == "fresh":
        ev["would"] += 1
        ev["claim"][hole] = now
        do = (f"WOULD send it through its own fresh hole {hole} at once, no hold ("
              + ("router probe" if by_probe and ok < SHADOW_PROVEN else f"fresh ok {ok}") + ", no-reply 0)")
    elif not free:
        do = "fresh holes proven, but none free: today's routing"
    else:
        ev["would"] += 1
        ev["claim"][free[0]] = now
        do = (f"WOULD move it to fresh hole {free[0]} at once ("
              + ("router probe" if by_probe and ok < SHADOW_PROVEN else f"fresh ok {ok}") + ", no-reply 0)")
    log(f"SHADOW link host={_hname(host)} ({rec['state']}) player={_who(ep[0])}:{ep[1]} kind={rec['kind']} ctx={rec['ctx']} "
        f"hole={hole} {_cls_txt(cls, idle)}; fresh holes free: {','.join(map(str, free[:6])) or 'none'}; {do}")


@_shadow_safe
def shadow_hint(addr, data: bytes, now: float) -> None:
    ip = addr[0]
    first = ip not in SHADOW_HINT
    last = SHADOW_HINT.get(ip, -1e9)
    SHADOW_HINT[ip] = now
    if first or now - last > 600:
        relayed = "a relayed host now" if ip in RELAY_HOSTS else "not hosting through the relay now"
        log(f"SHADOW hint: scopsDNS at {ip}:{addr[1]} says its router forwards the game ports (UPnP, public address); "
            f"{ip} is {relayed}. Log only - routing unchanged, and the datagram is not relayed to any player")


# ---- UPnP router probe (see UPNP_PROBE). Log only: it sends test packets to the asking PC and records what arrived. ----
def _probe_send(run: dict, kind: str, port: int, now: float) -> None:
    sock = run["socks"].get(port)
    import json as _json
    data = _json.dumps({"op": "upnp_probe_pkt", "id": run["id"], "kind": kind}).encode()
    try:
        for _ in range(3):                       # three copies: one lost datagram must not read as "router blocked"
            sock.sendto(data, (run["ip"], run["tport"]))
    except OSError as e:
        log(f"SHADOW probe {run['ip']}: send {kind} from :{port} failed: {e}")
    run["sent"][kind] = now
    PROBE_PORT_USE[(run["ip"], port)] = now


@_shadow_safe
def probe_request(addr, m: dict, now: float) -> None:
    """Returns a status for the window: started / busy / recent:<s> / off / not-public / bad-port / no-ports."""
    ip = addr[0]
    if not UPNP_PROBE:
        return "off"
    try:
        import ipaddress as _ip
        if not _ip.IPv4Address(ip).is_global and not os.environ.get("SCCT_RELAY_UPNP_PROBE_TEST"):
            log(f"SHADOW probe: request from {ip} ignored (not a public address)")
            return "not-public"
    except ValueError:
        return "not-public"
    if ip in PROBE_RUNS:
        return "busy"
    if now - PROBE_LAST.get(ip, -1e9) < PROBE_EVERY_S:
        return f"recent:{PROBE_EVERY_S - (now - PROBE_LAST[ip]):.0f}"
    try:
        tport = int(m.get("port", 3659))
    except (TypeError, ValueError):
        tport = 3659
    if not 1024 <= tport <= 65535:
        return "bad-port"
    busy = {p for r in PROBE_RUNS.values() for p in r["socks"]}          # another address's test is using them
    fresh = sorted((p for p in PROBE_PORTS if p not in busy and now - PROBE_PORT_USE.get((ip, p), -1e9) > 3600.0),
                   key=lambda p: PROBE_PORT_USE.get((ip, p), -1e9))
    socks, order, failed = {}, [], []
    for p in fresh:
        if len(order) == 2:
            break
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sk.bind(("0.0.0.0", p))
        except OSError as e:
            sk.close()
            failed.append(f"{p} ({e.strerror or e})")
            continue
        socks[p] = sk
        order.append(p)
    if len(order) < 2:
        for o in socks.values():
            o.close()
        log(f"SHADOW probe {ip}: not enough free probe ports (ports {PROBE_PORTS[0]}-{PROBE_PORTS[-1]}"
            + (f"; could not open {', '.join(failed)}" if failed else "") + "); probe skipped")
        return "no-ports"
    s_port, f_port = order[0], order[1]
    run = {"ip": ip, "tport": tport, "id": str(m.get("id", ""))[:32], "t0": now, "S": s_port, "F": f_port,
           "socks": socks, "plan": {"fresh1": (s_port, now), "fresh2": (f_port, now + 2.0), "stale": (s_port, None)},
           "sent": {}, "seen": {}, "res": {}}
    PROBE_RUNS[ip] = run
    log(f"SHADOW probe {_hname(ip)}:{tport} (id {run['id'] or '-'}): fresh test from :{s_port} now and from :{f_port} in 2s; "
        f"stale test from :{s_port} again {PROBE_STALE_S:.0f}s after the PC answers it. Log only")
    probe_tick(now)
    return "started"


@_shadow_safe
def probe_seen(addr, m: dict, now: float) -> None:
    run = PROBE_RUNS.get(addr[0])
    if run is None or str(m.get("id", ""))[:32] != run["id"]:
        return
    kind = str(m.get("kind", ""))
    if kind not in run["sent"] or kind in run["seen"]:
        return
    run["seen"][kind] = now
    if kind == "fresh1":
        port, _ = run["plan"]["stale"]
        run["plan"]["stale"] = (port, now + PROBE_STALE_S)   # the PC answered on that port just now: the flow is two-way


@_shadow_safe
def probe_tick(now: float) -> None:
    for ip, run in list(PROBE_RUNS.items()):
        for kind, (port, at) in run["plan"].items():
            if at is not None and kind not in run["sent"] and now >= at:
                _probe_send(run, kind, port, now)
        for kind, t in run["sent"].items():
            if kind in run["res"]:
                continue
            if kind in run["seen"]:
                run["res"][kind] = f"seen after {run['seen'][kind] - t:.1f}s"
            elif now - t >= PROBE_WAIT_S:
                run["res"][kind] = "NOT seen"
        stale_off = "fresh1" in run["res"] and "fresh1" not in run["seen"]
        done = (all(k in run["res"] for k in ("fresh1", "fresh2", "stale"))
                or (stale_off and "fresh2" in run["res"]) or now - run["t0"] > PROBE_STALE_S + 60.0)
        if not done:
            continue
        for sk in run["socks"].values():
            sk.close()
        PROBE_RUNS.pop(ip, None)
        PROBE_LAST[ip] = now
        res = run["res"]
        fresh_ok = sum(1 for k in ("fresh1", "fresh2") if k in run["seen"])
        fresh_miss = sum(1 for k in ("fresh1", "fresh2") if res.get(k) == "NOT seen")
        pr = _shadow_ev(ip).setdefault("probe", {"fresh_ok": 0, "fresh_miss": 0, "stale_ok": 0, "stale_miss": 0, "t": None})
        pr["fresh_ok"] += fresh_ok
        pr["fresh_miss"] += fresh_miss
        if "stale" in run["seen"]:
            pr["stale_ok"] += 1
        elif res.get("stale") == "NOT seen":
            pr["stale_miss"] += 1
        pr["t"] = now
        pr["last_fresh_ok"] = fresh_ok       # the proof rule judges the latest run (an earlier run may have been blocked
        pr["last_fresh_miss"] = fresh_miss   # by the PC's own firewall before the user allowed it)
        if fresh_ok == 2:
            verdict = "new connections reach this network through the forward"
        elif fresh_ok == 0:
            verdict = ("nothing arrived: the router did not forward the test port, or the PC's firewall blocked it "
                       "(not counted against the host)")
        else:
            verdict = "one of two fresh tests arrived (loss on the way, or a router that is not consistent)"
        stale = res.get("stale", "not run (no fresh answer to build on)")
        log(f"SHADOW probe result {_hname(ip)}:{run['tport']}: fresh from :{run['S']} {res.get('fresh1', '?')}, fresh from "
            f":{run['F']} {res.get('fresh2', '?')}; stale ({PROBE_STALE_S:.0f}s after a two-way exchange, from :{run['S']}) "
            f"{stale} -> {verdict} | probes so far: fresh seen {pr['fresh_ok']} / not seen {pr['fresh_miss']}, "
            f"stale seen {pr['stale_ok']} / not seen {pr['stale_miss']}")

def link_silence(now: float) -> None:
    """Log when a live player link stops in one direction and when it comes back (see SILENCE_LOG)."""
    if not SILENCE_LOG or not RELAY_HOLES:
        return
    for host, hv in RELAY_HOSTS.items():
        traffic = HOST_TRAFFIC.get(host, {})
        st = hv.setdefault("silence", {})
        for ep, (sock, last_rx) in list(hv["eps"].items()):
            if hv.get("ep_rx", {}).get(ep, 0) < SILENCE_MIN_RX:
                continue
            hole = hv.get("src", {}).get(ep)
            last_tx = traffic.get(hole, -1e9) if hole else -1e9
            j_quiet, h_quiet = now - last_rx, now - last_tx
            s0 = st.setdefault(ep, {"j": None, "h": None, "snap": []})
            # only links that were busy (in a match) count: lobby and menu traffic has long natural gaps
            rx, tx = hv.get("ep_rx", {}).get(ep, 0), hv.get("ep_tx", {}).get(ep, 0)
            snap = s0["snap"]
            snap.append((now, rx, tx))
            while len(snap) > 2 and now - snap[1][0] >= 5.0:
                snap.pop(0)
            if now - snap[0][0] >= 4.0:
                if rx - snap[0][1] >= 10:
                    s0["j_busy"] = now
                if tx - snap[0][2] >= 10:
                    s0["h_busy"] = now
            j_was_busy = now - s0.get("j_busy", -1e9) <= SILENCE_S + 2.0
            h_was_busy = now - s0.get("h_busy", -1e9) <= SILENCE_S + 2.0
            # player silent while the host keeps sending
            if s0["j"] is None and j_was_busy and j_quiet >= SILENCE_S and h_quiet < SILENCE_S:
                s0["j"] = last_rx
                log(f"RELAY silence: {_who(ep[0])}:{ep[1]} (hole {hole}) went silent {j_quiet:.1f}s ago; host {host} still sending "
                    f"(last {h_quiet:.1f}s ago, {hv.get('ep_rx', {}).get(ep, 0)} packets from the player so far)")
            elif s0["j"] is not None and j_quiet < 1.0:
                log(f"RELAY silence: {_who(ep[0])}:{ep[1]} (hole {hole}) sending again after {last_rx - s0['j']:.1f}s")
                s0["j"] = None
            elif s0["j"] is not None and j_quiet >= 60 and not s0.get("j_long"):
                s0["j_long"] = True
                log(f"RELAY silence: {_who(ep[0])}:{ep[1]} (hole {hole}) still silent after 60s (link ended)")
            if s0["j"] is None:
                s0.pop("j_long", None)
            # host silent while the player keeps sending
            if s0["h"] is None and h_was_busy and h_quiet >= SILENCE_S and j_quiet < 1.0 and last_tx > -1e9:
                s0["h"] = last_tx
                log(f"RELAY silence: host {host} stopped answering {_who(ep[0])}:{ep[1]} on hole {hole} {h_quiet:.1f}s ago; player still sending")
            elif s0["h"] is not None and h_quiet < 1.0:
                log(f"RELAY silence: host {host} answering {_who(ep[0])}:{ep[1]} on hole {hole} again after {last_tx - s0['h']:.1f}s")
                s0["h"] = None


def link_summary(now: float) -> None:
    """Every LINK_SUMMARY_S: one line per host flow with each joiner link's packets in/out since the last summary."""
    global LAST_SUMMARY
    if LINK_SUMMARY_S <= 0 or now - LAST_SUMMARY < LINK_SUMMARY_S:
        return
    LAST_SUMMARY = now
    for host, hv in RELAY_HOSTS.items():
        rx, tx = hv.setdefault("ep_rx", {}), hv.setdefault("ep_tx", {})
        prev = hv.setdefault("sum_prev", {})
        parts = []
        for ep in sorted(hv["eps"], key=lambda e: hv["eps"][e][1], reverse=True):
            r, t = rx.get(ep, 0), tx.get(ep, 0)
            pr, pt = prev.get(ep, (0, 0))
            prev[ep] = (r, t)
            # the counters restart when a joiner is reset; do not show that as a negative count
            dr, dt = (r - pr if r >= pr else r), (t - pt if t >= pt else t)
            if dr == 0 and dt == 0 and now - hv["eps"][ep][1] > LINK_SUMMARY_S:
                continue
            hole = hv.get("src", {}).get(ep, "?")
            parts.append(f"{ep[0]}:{ep[1]}@{hole} in={dr} out={dt} idle={now - hv['eps'][ep][1]:.0f}s")
        if parts:
            log(f"RELAY links host {host}: " + " | ".join(parts))


def relay_expire(now: float) -> None:
    for h, hv in list(RELAY_HOSTS.items()):
        if now - hv["last"] > RELAY_IDLE_S:
            RELAY_HOSTS.pop(h, None)
            HOST_EXT.pop(h, None)
            HOST_EXT_LOCK.pop(h, None)
            RELAY_TRAFFIC.pop(h, None)
            log(f"RELAY host {h}: idle {RELAY_IDLE_S:.0f}s, flow dropped (rx={hv['rx']} tx={hv['tx']})")


def warm_pick(clt, hv: dict, traffic: dict, now: float):
    """Next hole to warm for this host console, or (None, ""). See WARM_HOLES."""
    src = hv.get("src", {})
    eps = hv.get("eps", {})
    host = clt.addr
    joiners = {ip for ip, m in RELAY_MAP.items() if m.get("host") == host}
    by_ip: dict = {}
    for ep, port in src.items():
        if ep[0] in joiners and ep in eps and now - eps[ep][1] <= WARM_MAX_S:
            by_ip.setdefault(ep[0], []).append((eps[ep][1], ep, port))
    if not by_ip:
        return None, ""
    assigned = set(src.values())
    if len(by_ip) < WARM_FULL and getattr(clt, "warm_prev", False):
        spares = [p for p in RELAY_HOLES if p not in assigned and not hole_bad(host, p, now)]
        if spares and clt.hole_target in assigned:
            sp = max(spares, key=lambda p: traffic.get(p, 0.0))
            clt.warm_prev = False
            return sp, f"warm: back to spare {sp} ({len(by_ip)} joiner(s), room not full)"
    cands = []
    for ip, lst in by_ip.items():
        for last, ep, port in sorted(lst):            # least recently used socket first
            if now - last <= LINK_IDLE_S:
                continue                              # that link is live: its own traffic keeps the hole open
            if port == clt.hole_target or hole_bad(host, port, now):
                break
            age = now - traffic[port] if port in traffic else 1e9
            if age >= WARM_AGE_S:
                cands.append((age, port, ep, now - last))
            break                                     # one hole per joiner per round
    if not cands:
        return None, ""
    age, port, ep, idle = max(cands)
    clt.warm_prev = True
    fair_warm_steer(host, port, ep, now)
    return port, (f"warm: {ep[0]}:{ep[1]} idle {idle:.0f}s, host silent on {port} " +
                  ("(never opened)" if age >= 1e9 else f"for {age:.0f}s"))


def hole_steering(now: float) -> None:
    """Multi-hole mode. A steer (unsolicited PORT_ID for socket 1) is sent (a) once after login, to open the first hole,
    (b) when a joiner link is sending through a hole on which the host has been silent for HOLE_DEAD_S (never opened or
    expired), and (c) when the hole the console currently pings has been taken by a joiner link, to open the next spare.
    The console keeps only the hole it pings; established links keep theirs with their own traffic."""
    hold_detect(now)
    pool_tick(now)
    main = NAT_MAIN_SOCK
    for clt in list(CLIENTS):
        msg = getattr(clt, "hole_msg", None)
        if msg is None:
            continue
        if clt.hole_count >= HOLE_BUDGET:
            if not getattr(clt, "hole_budget_logged", False):
                clt.hole_budget_logged = True
                log(f"HOLE budget: {clt.addr} reached {HOLE_BUDGET} steering messages; no more steers this login")
            continue
        traffic = HOST_TRAFFIC.get(clt.addr, {})
        hv = RELAY_HOSTS.get(clt.addr)
        # serialize: the previous steer must have been confirmed by a ping (or be HOLE_MIN_S old) before the next one
        confirmed = clt.hole_target is None or traffic.get(clt.hole_target, 0.0) >= clt.hole_last
        failed = None
        if (NOREPLY_MOVE and not confirmed and now - clt.hole_last >= STEER_FAIL_S and now - clt.last_seen < 30
                and not hole_bad(clt.addr, clt.hole_target, now)):
            failed = clt.hole_target
            mark_bad(clt.addr, failed, now, f"steer #{clt.hole_count} to it got no ping in {now - clt.hole_last:.0f}s "
                                             f"while the console's other sockets kept pinging")
        strict = RELAY_HOLES and hold_state(clt.addr) == "strict"
        pool = strict and hv is not None and pool_on(clt.addr)
        clean_first = RELAY_HOLES and HOLD_MODE != "off"    # among spares, prefer one the relay has sent nothing into
        if (failed is None and strict and not confirmed and clt.hole_target is not None
                and hole_cold(clt.addr, clt.hole_target, now)
                and RELAY_COLD_SEND.get(clt.addr, {}).get(clt.hole_target, -1e9) >= clt.hole_last):
            # Packets went into the steered hole before its ping (the probe that showed this host is strict): on such a NAT
            # the ping is now blocked there, so steer to a clean hole at once instead of waiting STEER_FAIL_S. Not marked
            # bad: with nothing more sent into it, the hole is clean again after HOLD_CLEAN_S.
            failed = clt.hole_target
        if not confirmed and not failed and now - clt.hole_last < HOLE_MIN_S:
            continue
        # A steer only re-targets the console's 25 s ping cycle, so a second one inside that cycle wastes the first. "Confirmed"
        # above can be satisfied by game data on the hole, which is how steers ended up 12-15 s apart on 2026-11-06.
        if HOLE_ADAPTIVE and clt.hole_target is not None and now - clt.hole_last < HOLE_ADAPT_GAP_S:
            continue
        nat_st = HOST_NAT.get(clt.addr, {})
        open_nat = HOLE_ADAPTIVE and nat_st.get("open", False)
        rescue = False
        target = None
        why = ""
        if clt.hole_target is None:
            target, why = RELAY_HOLES[0], "login"
        else:
            if hv is not None:
                dead = []
                since = hv.get("joiner_since", {})
                for ep, port in hv.get("src", {}).items():
                    if ep not in hv["eps"] or now - hv["eps"][ep][1] > LINK_IDLE_S:
                        continue      # link not sending right now
                    if port == clt.hole_target or hole_bad(clt.addr, port, now):
                        continue      # already the ping target (or a hole that does not work for this host)
                    if strict and not relay_clean(clt.addr, port, now):
                        continue      # packets went in before a ping: a ping there would be blocked until it is clean
                    if open_nat and now - since.get(port, now) <= HOLE_OPEN_WAIT_S:
                        continue      # this host answers without a steer, usually within 10-14 s: give it the time
                    if port not in traffic:
                        dead.append((1e9, port, ep))   # never opened: steer at once
                    elif now - traffic.get(port, 0.0) > HOLE_DEAD_S and now - since.get(port, now) > HOLE_DEAD_S:
                        # A link that is actively sending but getting no host reply needs prompt rescue: if the host has
                        # been silent on its hole longer than HOLE_DEAD_S the mapping has really gone, so steer now. (At
                        # allocation we already prefer reusing a hole that is still open, so a healthy reuse never reaches
                        # here - the host's reply refreshes its traffic within HOLE_DEAD_S.)
                        dead.append((now - traffic.get(port, 0.0), port, ep))
                if dead:
                    age, target, ep = max(dead)
                    why = f"link {ep[0]}:{ep[1]} sending, host silent on {target} " + ("(never opened)" if age >= 1e9 else f"for {age:.0f}s")
                    rescue = open_nat
            if target is None and failed is not None:
                src_map = hv.get("src", {}) if hv is not None else {}
                live = {p for e, p in src_map.items() if e in hv["eps"] and now - hv["eps"][e][1] <= LINK_IDLE_S} if hv is not None else set()
                cand = [p for p in RELAY_HOLES if p != failed and p not in live and not hole_bad(clt.addr, p, now)]
                if cand:
                    target = max(cand, key=lambda p: (clean_first and relay_clean(clt.addr, p, now),
                                                      HOST_DATA.get(clt.addr, {}).get(p, 0.0), traffic.get(p, 0.0)))
                    why = (f"packets went into {failed} before its ping (strict NAT); clean spare" if strict and
                           not hole_bad(clt.addr, failed, now) else f"steer to {failed} failed; new spare")
            # A ready spare is what gives a new link an instant reply (a join gives up after about 5 s), so an open-NAT host
            # still gets spares, but only while enough of its ~11 honoured steers remain for rescues.
            if target is None and pool and (confirmed or now - clt.hole_last >= HOLE_MIN_S):
                target, why = pool_pick(clt, hv, traffic, now)
            if target is None and confirmed and hv is not None and not pool and not (open_nat and clt.hole_count >= HOLE_ADAPT_SPARE_MAX):
                # Keep one spare hole the console pings, so the next NEW joiner link gets an instant reply. But steer
                # only when no already-open hole could serve that link, because every steer spends the console's scarce
                # budget (~11 per login). An open hole (host pinging it, or host sent <HOLE_DEAD_S ago) whose occupants
                # are all idle is reusable with no steer - so during a lobby/rematch lull there is no need to steer.
                src_map = hv.get("src", {})
                assigned = set(src_map.values())
                def all_idle(pt):
                    return all(e not in hv["eps"] or now - hv["eps"][e][1] > LINK_IDLE_S
                               for e in src_map if src_map[e] == pt)
                have_ready = any(
                    (pt == clt.hole_target or (traffic.get(pt, 0.0) > 0 and now - traffic.get(pt, 0.0) < HOLE_REUSE_S))
                    and (pt not in assigned or all_idle(pt))
                    for pt in RELAY_HOLES)
                if clt.hole_target in assigned and not all_idle(clt.hole_target) and not have_ready:
                    nxt = [p for p in RELAY_HOLES if p not in assigned and not hole_bad(clt.addr, p, now)]
                    if nxt:
                        target = min(nxt, key=lambda p: (clean_first and not relay_clean(clt.addr, p, now), traffic.get(p, 0.0)))
                        why = f"spare: hole {clt.hole_target} now carries a live link and no open hole is free"
        if target is None and WARM_HOLES and hv is not None and now - clt.hole_last >= (WARM_GAP_S if confirmed else 2 * WARM_GAP_S):
            target, why = warm_pick(clt, hv, traffic, now)
        elif target is not None:
            clt.warm_prev = False
        if target is None:
            continue
        if rescue:
            nat_st["misses"] = nat_st.get("misses", 0) + 1
            if nat_st["misses"] >= HOLE_OPEN_MISSES:
                nat_st.update(open=False, hits=0, misses=0)
                log(f"HOLE adaptive: {clt.addr} stayed silent {HOLE_OPEN_MISSES} times without a steer; back to normal steering")
        try:
            xr, xpid = nat_reply(msg, gsm.NAT_MSG.PORT_ID, 0, target, pick_id=True)
            clt.reqs[xpid] = {"msg": msg, "sock": "1", "pending": True, "extra": True, "port": target, "ts": now}
            main.sendto(bytes(srp.SRPResponse(None, clt, xr)), (clt.addr, clt.port))
            clt.hole_target = target
            clt.hole_last = now
            clt.hole_count += 1
            if not hasattr(clt, "hole_steer_time"):
                clt.hole_steer_time = {}
            clt.hole_steer_time[target] = now
            ka_n = (hv or {}).get("ka", {}).get(target, 0) if hv else 0
            ka_t = RELAY_TRAFFIC.get(clt.addr, {}).get(target)
            log(f"HOLE steer #{clt.hole_count}: {clt.addr} socket 1 -> ping port {target} ({why}; " +
                ", ".join(f"{p}: {now - traffic[p]:.0f}s ago" if p in traffic else f"{p}: never" for p in RELAY_HOLES if p in traffic or p == target) +
                f"; keepalives on {target}: {ka_n}" + (f", last relay send {now - ka_t:.0f}s ago" if ka_t else "") + ")")
            if clt.hole_count >= HOLE_BUDGET - 2:
                log(f"HOLE WARNING: {clt.addr} is near the steer budget: {clt.hole_count}/{HOLE_BUDGET} this login")
            if clt.hole_count == HOLE_GAME_LIMIT + 1:
                log(f"HOLE count: {clt.addr} passed {HOLE_GAME_LIMIT} steers this login (the old limit, before the SRP wrap fix). "
                    f"Check that its next acks wrap to 0x0 and that steer #{clt.hole_count} gets a HOLE PING")
        except Exception as e:
            log(f"HOLE steer failed for {clt.addr}: {e!r}")
        old = [k for k, v in clt.reqs.items() if v.get("extra") and now - v.get("ts", now) > 600]
        for k in old:
            clt.reqs.pop(k, None)


def hole_keepalive(now: float) -> None:
    """Keep every hole that has carried host traffic open by sending a datagram inbound through it when quiet."""
    for host in list(HOST_TRAFFIC):
        hv = RELAY_HOSTS.get(host) or KA_STATE.setdefault(host, {})
        traffic = HOST_TRAFFIC.get(host, {})
        sent = RELAY_TRAFFIC.setdefault(host, {})
        ext = HOST_EXT.get(host, {})
        for port in RELAY_HOLES:
            if port not in traffic or port not in ext:
                continue   # hole never carried host traffic: nothing to keep alive yet
            if pool_on(host) and not hole_bad(host, port, now):
                lf = POOL[host]["life"].get(port) if host in POOL else None
                age = now - traffic[port]
                due = None
                if lf is None or lf[1] is None:
                    if age <= POOL_ASSURE_MAX_AGE_S:      # just opened by the host: make it two-way (two packets)
                        due = lf is None or now - lf[0] >= POOL_ASSURE_GAP_S
                elif age <= pool_trust(host):
                    due = now - sent.get(port, -1e9) >= POOL_KA_S
                if due is not None:
                    if due:
                        try:
                            HOLE_SOCKS[port].sendto(HOLE_KEEPALIVE_PAYLOAD, (host, ext[port]))
                            sent[port] = now
                            was = lf is not None and lf[1] is not None
                            pool_sent(host, port, now)
                            st = POOL[host]
                            lf = st["life"].get(port)
                            if not was and lf and lf[1] is not None and st["logged"] < 6:
                                st["logged"] += 1
                                log(f"POOL: host {host} hole {port} made two-way {age:.0f}s after the host's packet; kept open "
                                    f"with a {len(HOLE_KEEPALIVE_PAYLOAD)}B keepalive every {POOL_KA_S:.0f}s")
                        except OSError as e:
                            log(f"HOLE keepalive from :{port} failed: {e}")
                    continue
            if HOLD_MODE != "off" and hole_cold(host, port, now):
                continue   # long dead: a keepalive cannot revive it, and on a strict NAT it would block the next ping there
            last = max(traffic.get(port, 0.0), sent.get(port, 0.0))
            if now - last < HOLE_KEEPALIVE_S:
                continue
            try:
                HOLE_SOCKS[port].sendto(HOLE_KEEPALIVE_PAYLOAD, (host, ext[port]))
                sent[port] = now
                n = hv.setdefault("ka", {}).get(port, 0) + 1
                hv["ka"][port] = n
                if n <= 2 or n % 50 == 0:
                    log(f"HOLE keepalive #{n}: {len(HOLE_KEEPALIVE_PAYLOAD)}B from :{port} -> {host}:{ext[port]} (hole quiet {now - last:.0f}s)")
            except OSError as e:
                log(f"HOLE keepalive from :{port} failed: {e}")


def pick_slot(hv: dict, ep, now: float, prev_t, nslots: int) -> int:
    """One dedicated hole slot per joiner endpoint (ip, port), kept until the joiner is reset (GROUP_LEAVE) or the flow
    expires. This mirrors a direct path: the host keys its peer by the joiner's (ip, port) for the whole session and copes
    by itself with the client reusing a port across roles (probe link -> game link), so the relay must present one fixed
    source port per joiner endpoint. The earlier "share slot 0 until established / re-evaluate after idling" rule broke
    both joins on 2026-11-05 15:09: COOLDOR's room link (3 packets, then quiet 7 s - normal in the lobby) was moved from
    hole 45001 to 45002 when it resumed, so the host saw a new unknown peer and COOLDOR timed out; and DORDOR's join
    request was put on 45001 as well, where the host already had COOLDOR's peer, and the replies of both were misrouted."""
    slots = hv.setdefault("slot", {})
    if ep in slots:
        return slots[ep]
    held = set(slots.values())
    free = [i for i in range(nslots) if i not in held]
    if free:
        slot = free[0]
    else:
        # exhausted: take over the slot of the endpoint idle the longest (a probe link that finished long ago, or a
        # peer the host has dropped since)
        lru = min(slots, key=lambda e: hv["eps"][e][1] if e in hv["eps"] else 0.0)
        slot = slots.pop(lru)
        idle = now - hv["eps"][lru][1] if lru in hv["eps"] else 0.0
        hv.get("src", {}).pop(lru, None)
        hv.get("src_logged", {}).pop(lru, None)
        log(f"RELAY host: all {nslots} holes in use, {ep[0]}:{ep[1]} takes over hole slot {slot} from {lru[0]}:{lru[1]} (idle {idle:.0f}s)")
    slots[ep] = slot
    return slot


def host_nat_client(host: str):
    """The NatClient (SRP session on 10070) of a relayed host console, if it is logged in."""
    for c in CLIENTS:
        if c.addr == host and getattr(c, "hole_msg", None) is not None:
            return c
    return None


def noreply_move(hv: dict, host: str, ep, now: float):
    """See NOREPLY_MOVE. Returns the new hole for ep, or None to keep the current one."""
    src = hv.get("src", {})
    old = src.get(ep)
    c = hv.get("conn", {}).get(ep)
    if old is None or c is None or c["moves"] >= NOREPLY_MAX_MOVES or c["n"] < NOREPLY_MIN_PKTS:
        return None
    if (ep, old) in hv.get("held", {}):
        return None                                  # held for the host's ping (strict NAT): nothing was sent, nothing to judge
    hd = HOST_DATA.get(host, {})
    if hd.get(old, -1e9) >= c["start"]:
        return None                                  # the host has answered this connection: never move it
    others = [t for p, t in hd.items() if p != old]
    if not others or now - max(others) > NOREPLY_HOST_ALIVE_S:
        return None                                  # host not sending anywhere right now (loading a map): wait
    if now - max(c["start"], hv.get("host_active_since", now)) < NOREPLY_S:
        return None
    # unanswered NOREPLY_S while the host answers others: on a hole dead at the link's start that decides an untested host
    # (before the steer wait below: a router that lets players in answers whether or not a ping is on its way)
    hold_noreply_verdict(host, old, c["start"], now, f"{_who(ep[0])}:{ep[1]}")
    sc = host_nat_client(host)
    proven_only = False
    if sc is not None and sc.hole_target == old and HOST_TRAFFIC.get(host, {}).get(old, -1e9) < sc.hole_last \
            and now - sc.hole_last < STEER_FAIL_S:
        if not (RESCUE_NOWAIT and sc.hole_last > c["start"]):
            return None                              # the host was just steered here and has not pinged yet: wait for it
        # a rescue steer for a link already sending into this hole: its ping may be blocked (see RESCUE_NOWAIT), so do
        # not wait - but only move to a hole the host has really answered on lately, never to another guess
        proven_only = True
    if now - HOST_TRAFFIC.get(host, {}).get(old, -1e9) < NOREPLY_S:
        return None                                  # the host's ping just opened this hole: count from there
    # the player's other links on this hole that are just as unanswered move with it (join probe + game link)
    group = [e for e, p in src.items() if p == old and e[0] == ep[0] and e in hv["eps"]
             and now - hv["eps"][e][1] <= LINK_IDLE_S and e not in hv.get("leaving", {})
             and hd.get(old, -1e9) < hv.get("conn", {}).get(e, {"start": now})["start"]]
    traffic = HOST_TRAFFIC.get(host, {})
    clt = host_nat_client(host)
    def live_elsewhere(pt):
        return any(p == pt and e not in group and holds_hole(hv, e, ep, now) for e, p in src.items())
    best, best_key, kind = None, None, ""
    for pt in RELAY_HOLES:
        if pt == old or hole_bad(host, pt, now) or live_elsewhere(pt):
            continue
        proven = now - hd.get(pt, -1e9) <= HOLE_REUSE_S
        pinged = clt is not None and pt == clt.hole_target and traffic.get(pt, 0.0) >= clt.hole_last \
            and now - traffic.get(pt, -1e9) <= 30
        # on a strict host a hole the host pinged within HOLD_PING_OPEN_S is open on its router (that is what a ping
        # does there), and nothing has been sent into it since: as good as a proven one for a rescue. 2026-11-15 06:40:20
        # (Factory revenge swap): the only proven holes were in use, the freshly pinged spare 45010 did not count, and
        # ANDROID-DOR's room link waited 35 s on a dead hole for a ping that was blocked.
        fresh = (RESCUE_FRESH and hold_state(host) == "strict" and pt in traffic and now - traffic[pt] <= HOLD_PING_OPEN_S
                 and relay_clean(host, pt, now) and not hole_cold(host, pt, now))
        key = (2 if proven or fresh else 1 if pinged else 0, traffic.get(pt, 0.0))
        if best_key is None or key > best_key:
            best, best_key = pt, key
            kind = ("answered there " + f"{now - hd[pt]:.0f}s ago" if proven else
                    f"the host pinged it {now - traffic[pt]:.0f}s ago (open on a strict NAT)" if fresh else "the host is pinging it" if pinged
                    else f"host last sent {now - traffic[pt]:.0f}s ago" if pt in traffic else "never used, steer follows")
    if best is None or (proven_only and best_key[0] < 2):
        return None
    stale =[e for e, p in src.items() if p == best and e not in group]
    for e in stale:                                  # idle earlier occupants: the hole changes hands, as on reuse
        src.pop(e, None)
        hv.get("probe", set()).discard(e)
    for e in group:
        src[e] = best
        ce = hv.setdefault("conn", {}).get(e, {"moves": 0})
        hv["conn"][e] = {"start": now, "n": 0, "moves": ce["moves"] + 1}
    hv.setdefault("joiner_since", {}).pop(best, None)
    fail_why(hv, host, old, c, group, now)
    hold_downgrade(host, f"hole {old} got no reply for {now - c['start']:.0f}s while it answered others")
    mark_bad(host, old, now, f"{_who(ep[0])} sent {c['n']} packets through it in {now - c['start']:.0f}s and got no reply "
                             f"while the host was sending on other holes")
    log(f"RELAY host {host}: no reply on hole {old} -> moved {', '.join(f'{e[0]}:{e[1]}' for e in group)} to hole {best} ({kind})")
    for e in group:
        age_note(host, e, now, f"no reply on {old}, moved to {best}", hole=best)
    return best


def pick_hole_port(hv: dict, ep, now: float, prev_t=None, host: str = None) -> int:
    """Source hole port for a joiner endpoint (ip, port).

    Facts this is built on (2026-11-05): the host's NAT keeps a hole only while the HOST sends through it - its 3658 socket
    pings exactly one hole (the last PORT_ID target) every 25 s, and a hole it stopped pinging is gone after ~60 s; the
    relay's inbound keepalives do not refresh it. A steer (PORT_ID) costs budget (~10 per login) and takes effect at the
    console's next ping, up to 25 s later. A link that the host answers keeps its own hole open with its game traffic.

    So: one hole per endpoint for good (the host keys the peer by (ip, port) and never sees it move), a new endpoint takes
    the READY hole (the one the host is pinging right now, unassigned), after which hole_steering steers the console to the
    next spare. Exceptions: a joiner's second link right behind its join probe shares the probe's hole (the host copes with
    the two from one source; this keeps the spare for the next joiner and saves a steer), and the probe's port, when the
    client reuses it later for another link, gets a hole of its own again."""
    src = hv.setdefault("src", {})
    probes = hv.setdefault("probe", set())
    first = hv.setdefault("first_seen", {})
    traffic = HOST_TRAFFIC.get(host, {}) if host else {}
    strict_h = bool(host) and HOLD_MODE != "off" and hold_state(host) == "strict"
    if ep in src and strict_h and pool_on(host) and hole_cold(host, src[ep], now) and ep not in hv.get("leaving", {}) \
            and ((ep, src[ep]) in hv.get("held", {}) or prev_t is None or now - prev_t > LINK_IDLE_S):
        # A link coming back (map load) on a hole that has gone cold, or one already waiting for a ping: nothing of this
        # connection has reached the host, so it can start on a ready spare instead of waiting for the next ping.
        sp = pool_spare(hv, host, ep, now, skip=src[ep])
        if sp is not None:
            old = src[ep]
            for e in [e for e, p in src.items() if p == sp and e != ep]:
                src.pop(e, None)
                probes.discard(e)
            src[ep] = sp
            waited = hv.get("held", {}).pop((ep, old), None)
            hv.setdefault("joiner_since", {}).pop(sp, None)
            c = hv.get("conn", {}).get(ep)
            if c is not None:
                c["start"], c["n"] = now, 1
            pool_use(host, sp, ep, now)
            log(f"RELAY host {host}: {_who(ep[0])}:{ep[1]} " + (f"waited {now - waited[0]:.0f}s on" if waited else "is back on")
                + f" cold hole {old} -> ready spare {sp} (host sent there {now - traffic.get(sp, now):.0f}s ago)")
            age_note(host, ep, now, f"cold {old} -> ready spare {sp}", hole=sp)
            return sp
    if ep in src and strict_h and (ep, src[ep]) in hv.get("held", {}):
        # Held link (nothing of it has reached the host yet): it follows the console's ping, so it is let through as soon
        # as that ping lands. 2026-11-14 23:01:44: a held link stayed on the hole the probe had blocked while the ping was
        # steered elsewhere, and waited 59 s (until a rescue steer came back to it) - right before a match start.
        sc = host_nat_client(host)
        tgt = sc.hole_target if sc is not None else None
        if tgt is not None and tgt != src[ep] and not hole_bad(host, tgt, now):
            busy = [e for e, p in src.items() if p == tgt and e != ep and holds_hole(hv, e, ep, now)]
            if REUSE_OTHER_QUIET_S > 0 and any(e[0] != ep[0] for e, p in src.items() if p == tgt and e != ep) \
                    and now - HOST_DATA.get(host, {}).get(tgt, -1e9) < REUSE_OTHER_QUIET_S:
                busy = busy or ["other player's hole, host still talking there"]
            if not busy:
                old = src[ep]
                for e in [e for e, p in src.items() if p == tgt and e != ep]:
                    src.pop(e, None)
                    probes.discard(e)
                src[ep] = tgt
                hv["held"][(ep, tgt)] = hv["held"].pop((ep, old))
                hv.setdefault("joiner_since", {}).pop(tgt, None)
                log(f"RELAY host {host}: held {_who(ep[0])}:{ep[1]} moves from hole {old} to {tgt}, the hole the host pings next")
                age_note(host, ep, now, f"held, follows the ping {old} -> {tgt}", hole=tgt)
                return tgt
    if ep in src:
        if ep in probes and prev_t is not None and now - prev_t > LINK_IDLE_S:
            old = src.pop(ep)
            probes.discard(ep)
            log(f"RELAY host {host}: join-probe port {ep[0]}:{ep[1]} reused after {now - prev_t:.0f}s idle; leaves shared hole {old} for one of its own")
        elif WAKE_MOVE and prev_t is not None and now - prev_t >= WAKE_IDLE_S and now - traffic.get(src[ep], -1e9) > WAKE_COLD_S:
            old = src[ep]
            def busy(pt):
                return any(e != ep and p == pt and holds_hole(hv, e, ep, now) for e, p in src.items())
            warm = [pt for pt in RELAY_HOLES if pt != old and now - traffic.get(pt, -1e9) <= WAKE_WARM_S and not busy(pt)]
            if not warm:
                return old
            mine = [pt for pt in warm if any(e[0] == ep[0] and p == pt for e, p in src.items())]
            new = max(mine or warm, key=lambda pt: traffic.get(pt, 0.0))
            src[ep] = new
            hv.setdefault("joiner_since", {}).pop(new, None)
            log(f"RELAY host {host}: {ep[0]}:{ep[1]} back after {now - prev_t:.0f}s (reconnect); its hole {old} is cold "
                f"(host silent {now - traffic.get(old, -1e9):.0f}s) -> moved to warm hole {new} "
                f"(host sent {now - traffic.get(new, 0.0):.0f}s ago{', its previous hole' if mine else ''})")
            return new
        else:
            if NOREPLY_MOVE and host and ep not in hv.get("leaving", {}):
                moved = noreply_move(hv, host, ep, now)
                if moved is not None:
                    return moved
            return src[ep]
    first[ep] = now
    if REBIND_FOLLOW and host:
        # See REBIND_FOLLOW: the player's newest, young link paused a few seconds ago and the same IP is back from a new
        # port - most likely the same game socket behind a NAT/VPN that gave it a new port. Keep its hole, so the host's
        # game sees the connection continue from the same relay address.
        mine = [e for e in src if e[0] == ep[0] and e != ep and e in hv["eps"]]
        if mine:
            e = max(mine, key=lambda o: first.get(o, 0.0))
            p = src[e]
            quiet = now - hv["eps"][e][1]
            age = hv["eps"][e][1] - first.get(e, now)
            busy = any(o != e and q == p and o in hv["eps"] and now - hv["eps"][o][1] <= LINK_IDLE_S
                       for o, q in src.items())
            if (LINK_IDLE_S < quiet <= REBIND_S and 0 <= age <= REBIND_MAX_AGE_S and not busy
                    and e not in hv.get("leaving", {}) and (e, p) not in hv.get("held", {}) and not hole_bad(host, p, now)
                    and now - HOST_DATA.get(host, {}).get(p, -1e9) <= REBIND_S + 5.0):
                src.pop(e, None)
                probes.discard(e)
                src[ep] = p
                log(f"RELAY host {host}: {_who(ep[0])} is back from port {ep[1]} {quiet:.1f}s after its {age:.0f}s-old link "
                    f"{e[0]}:{e[1]} paused (new port for the same socket?): keeps that link's hole {p}")
                return p
    assigned = set(src.values())
    clt = host_nat_client(host) if host else None
    target = clt.hole_target if clt is not None else None
    if target is not None and hole_bad(host, target, now):
        target = None
    ready = target is not None and target not in assigned and traffic.get(target, 0.0) >= clt.hole_last
    port, why = None, ""
    # same joiner, second link within 3 s of its join probe (which has sent only a couple of packets): share the probe's hole
    for e, p in src.items():
        if e[0] == ep[0] and e not in probes and now - first.get(e, 0.0) <= 3.0 and hv.get("ep_rx", {}).get(e, 0) <= 4 \
                and list(src.values()).count(p) == 1 and traffic.get(p, 0.0) > 0:
            port, why = p, f"shares the hole of join probe {e[0]}:{e[1]}"
            probes.add(e)
            break
    if port is None and ready:
        port, why = target, "ready: the host is pinging it (unassigned)"
    if port is None:
        # Reuse an already-OPEN hole whose occupants are all idle, with no steer. A hole the host is pinging now
        # (clt.hole_target) or has sent through within HOLE_DEAD_S is open on the host NAT; if the link(s) assigned to
        # it have gone quiet (everyone back in the lobby for a rematch, or an old match link finished) a new link can
        # take it and the host answers at once - sequential links on one hole do not collide (routing follows the last
        # sender). This is what lets a long session keep going past the console's ~11-steer ceiling: rematches land on
        # holes that are already open instead of each demanding a fresh steer the console will eventually ignore.
        def occupants(pt):
            return [e for e in src if src[e] == pt and e != ep]
        def all_idle(pt):
            return not any(holds_hole(hv, e, ep, now) for e in occupants(pt))
        reusable = []
        hdata = HOST_DATA.get(host, {}) if host else {}
        for pt in RELAY_HOLES:
            if not all_idle(pt) or hole_bad(host, pt, now):
                continue
            if REUSE_OTHER_QUIET_S > 0 and any(e[0] != ep[0] for e in occupants(pt)) \
                    and now - hdata.get(pt, -1e9) < REUSE_OTHER_QUIET_S:
                continue                              # another player's link the host may still be talking to
            # the ping target only counts once its ping has arrived (2026-11-13 04:05: rejoins were sent to a steered
            # hole whose ping never came)
            is_ping = target == pt and (not NOREPLY_MOVE or traffic.get(pt, 0.0) >= clt.hole_last)
            host_open = traffic.get(pt, 0.0) > 0 and now - traffic.get(pt, 0.0) < HOLE_REUSE_S
            if host_open and strict_h and hole_cold(host, pt, now):
                host_open = False                 # e.g. a spare pinged 30-50 s ago: gone on a strict NAT, and sending blocks it
            if strict_h and pool_alive(host, pt, now):
                host_open = True                  # a kept spare of the pool
            if is_ping or host_open:
                mine = not strict_h or not pool_on(host) or all(e[0] == ep[0] for e in occupants(pt))
                reusable.append((is_ping, mine, traffic.get(pt, 0.0), pt))
        if reusable:
            reusable.sort(reverse=True)   # prefer the ping target, then the most recently open
            port = reusable[0][3]
            popped = occupants(port)
            for e in popped:               # drop the stale idle occupant(s); the new link owns the hole now
                src.pop(e, None); probes.discard(e)
            if FAIR_SHADOW and host:
                fair_reuse(host, port, ep, popped, [r[3] for r in reusable[1:]], now)
            why = "reuses open hole the host is pinging" if reusable[0][0] else f"reuses open hole (host sent {now - reusable[0][2]:.0f}s ago)"
            if strict_h and not reusable[0][0] and pool_alive(host, port, now):
                why = f"ready spare, kept open by keepalives (host sent {now - reusable[0][2]:.0f}s ago)"
                pool_use(host, port, ep, now)
    if port is None and target is not None and target not in assigned:
        port, why = target, f"steered {now - clt.hole_last:.0f}s ago, waiting for the host's ping"
    if port is None:
        free = [p for p in RELAY_HOLES if p not in assigned and not hole_bad(host, p, now)] or \
               [p for p in RELAY_HOLES if p not in assigned]
        if free:
            port = min(free, key=lambda p: traffic.get(p, 0.0))
            why = "free hole, needs a steer" + (" (never pinged)" if port not in traffic else f" (host silent {now - traffic[port]:.0f}s)")
        else:
            lru = min(src, key=lambda e: hv["eps"][e][1] if e in hv["eps"] else 0.0)
            port = src.pop(lru)
            probes.discard(lru)
            idle = now - hv["eps"][lru][1] if lru in hv["eps"] else 0.0
            why = f"all {len(RELAY_HOLES)} holes in use, taken over from {lru[0]}:{lru[1]} (idle {idle:.0f}s)"
    src[ep] = port
    log(f"RELAY host {host}: joiner link {ep[0]}:{ep[1]} <-> host:{RELAY_HOST_PORT} via hole port {port} ({why})")
    if FAIR_SHADOW and host:
        fair_back(host, ep, port, why, now)
    return port


def handle_packet(data: bytes, addr: tuple[str, int], now: float, nat_port: int):
    if UPNP_HINT and data[:1] == b"{" and len(data) <= 256 and b"upnp_" in data:
        import json as _json
        try:
            m = _json.loads(data.decode("utf-8"))
        except Exception:
            m = None
        op = m.get("op") if isinstance(m, dict) else None
        if op == "upnp_open":
            shadow_hint(addr, data, now)
            return []
        if op == "upnp_probe":
            status = probe_request(addr, m, now) or "error"
            # answered from 45000 on the flow the PC just opened, so its router and firewall let it in
            reply = _json.dumps({"op": "upnp_probe_reply", "id": str(m.get("id", ""))[:32], "status": status,
                                 "stale_s": PROBE_STALE_S}).encode()
            return [(reply, addr)]
        if op == "upnp_probe_seen":
            probe_seen(addr, m, now)
            return []
    if len(data) == 4:
        return handle_ping(data, addr, now, nat_port)
    # Relayed host: anything from it that is not a 4-byte NAT ping and does not come from its SRP control port is game
    # data for its joiner. Classification is by SOURCE PORT only: the game's SRP/NAT session always runs on 10070, the
    # game sockets (3658, 10071-10073) only ever send pings to this service. Inspecting the scrambled game payload for
    # SRP flag bits misfired on 2026-11-04 16:48 (a game packet "looked like" an SRP SYN, the host's game socket was
    # registered as a NAT client and every later packet from it went to the SRP parser instead of the joiner).
    if RELAY_HOSTS and addr[0] in RELAY_HOSTS and addr[1] != RELAY_SRP_PORT:
        stale = client.NatClient.find(addr, CLIENTS)
        if stale is not None and SRP_BY_SESSION and (getattr(stale, "reqs", None) or getattr(stale, "hole_msg", None) is not None):
            stale = "session"        # the host's real SRP/NAT session, moved off 10070 by its router: not game data
        elif stale is not None:
            CLIENTS.remove(stale)
            log(f"RELAY: dropped stray NAT-client entry for game socket {addr[0]}:{addr[1]}")
            stale = None
        if stale is None and relay_from_host(data, addr, now, nat_port):
            return []
    if len(data) < srp.SRP_HEADER_SIZE:
        log(f"ignored {len(data)}B datagram from {addr[0]}:{addr[1]}: {data.hex()}")
        return []
    return handle_segment(data, addr, now, nat_port)


def main(argv=None) -> int:
    global LOG_FP
    ap = argparse.ArgumentParser(description="GSNAT responder v3")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ports", default="45000")
    ap.add_argument("--max-bytes", type=int, default=1024)  # accepted for compatibility
    ap.add_argument("--reply-mode", default="srp_nat")      # accepted for compatibility
    ap.add_argument("--log-file", default="logs/udp_45000_reply.log")
    ap.add_argument("--relay-ports", default=os.environ.get("SCCT_RELAY_PORTS", ""),
                    help="UDP relay: joiner-facing ports, e.g. 3658,10070-10080 (empty = relay off)")
    ap.add_argument("--relay-ctrl-port", type=int, default=int(os.environ.get("SCCT_RELAY_CTRL_PORT", "45099")),
                    help="loopback UDP control port the lobby server talks to (default 45099)")
    args, _ = ap.parse_known_args(argv)
    global RELAY_PORTS, RELAY_CTRL_PORT, NAT_MAIN_SOCK
    if args.log_file:
        p = pathlib.Path(args.log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        LOG_FP = open(p, "a", encoding="utf-8", errors="replace")
    ports = [int(x) for x in str(args.ports).replace(";", ",").split(",") if x.strip()]
    socks = {}
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((args.bind, port))
        socks[s] = port
        if NAT_MAIN_SOCK is None:
            NAT_MAIN_SOCK = s
    relay_socks: dict = {}
    ctrl_sock = None
    global HOLE_SOCKS
    for hp in RELAY_HOLES:
        if hp not in socks.values():
            hs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            hs.bind((args.bind, hp))
            socks[hs] = hp
        HOLE_SOCKS[hp] = next(sk for sk, pt in socks.items() if pt == hp)
    RELAY_PORTS = _parse_port_list(args.relay_ports) if str(args.relay_ports).strip() else []
    if RELAY_PORTS:
        RELAY_CTRL_PORT = int(args.relay_ctrl_port)
        for port in RELAY_PORTS:
            rs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rs.bind((args.bind, port))
            relay_socks[rs] = port
        ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        ctrl_sock.bind(("127.0.0.1", RELAY_CTRL_PORT))
    log("GSNAT v3 listening")
    log(f"  PORT_ID limit: {PORTID_LIMIT or 'all'}  gap: {GAP_S*1000:.0f} ms")
    log(f"  ADDRESS delay: {ADDRESS_DELAY_S*1000:.0f} ms  repeat-ping ACKs: {'off' if NO_REPEAT_ACK else 'on'}  ADDRESS: {'NEVER' if NO_ADDRESS else 'sent'}  even-length datagrams: {'on' if EVEN_LEN else 'off'}")
    log(f"  bind: {args.bind}  ports: {', '.join(str(p) for p in ports)}")
    log(f"  ubi-gs modules: {NEW_UBIGS}")
    if RELAY_PORTS:
        log(f"  RELAY: joiner ports {', '.join(str(p) for p in RELAY_PORTS)} -> host:{RELAY_HOST_PORT} from :{socks[NAT_MAIN_SOCK]}; control 127.0.0.1:{RELAY_CTRL_PORT}; idle {RELAY_IDLE_S:.0f}s; dump {RELAY_DUMP} pkts/dir; fanout {RELAY_FANOUT_S:.0f}s; alt host ports {RELAY_ALT_HOST_PORTS or 'off'}")
    if NAT_EXTRA_PORTID and not RELAY_HOLES:
        log(f"  EXTRA PORT_ID experiment: socket 1 also told to ping {NAT_EXTRA_PORTID} (these ports must be in --ports)")
    if RELAY_HOLES:
        log(f"  MULTI-HOLE: {len(RELAY_HOLES)} holes; one hole per joiner endpoint; the console pings one spare hole, a new link takes it and the next spare is steered; steer also when a sending link finds a hole never opened / host silent >{HOLE_DEAD_S:.0f}s (budget {HOLE_BUDGET}); {len(HOLE_KEEPALIVE_PAYLOAD)}B keepalives every {HOLE_KEEPALIVE_S:.0f}s")
        if WARM_HOLES:
            log(f"  WARM HOLES: on - the console's ping is steered round each joiner's least recently used hole (one steer per {WARM_GAP_S:.0f}s, holes quiet >{WARM_AGE_S:.0f}s, spare kept while fewer than {WARM_FULL} joiners)")
        log(f"  LEAVE GRACE: {'on' if LEAVE_GRACE else 'off'} - a link still sending at a room leave keeps its hole until quiet "
            f"{LEAVE_IDLE_S:.0f}s; a join forgets the player's idle links so the join starts on one hole")
        log(f"  NO-REPLY MOVE: {'on' if NOREPLY_MOVE else 'off'} - an unanswered link ({NOREPLY_S:.0f}s, host sending elsewhere) "
            f"moves to a working hole; failed steers ({STEER_FAIL_S:.0f}s, no ping) and such holes are avoided {BAD_HOLE_S:.0f}s; "
            f"rescue no-wait {'on' if RESCUE_NOWAIT else 'off'} (a steer made after the link was already sending there is not "
            f"waited for when another hole is proven)")
        log(f"  SRP BY SESSION: {'on' if SRP_BY_SESSION else 'off'} - a host's SRP/NAT session is recognised on whatever port its "
            f"router gives it (not only {RELAY_SRP_PORT}), so steering keeps working for hosts behind port-rewriting routers")
        log(f"  OPEN VERDICT: " + (f"strict - map-load burst answers ignored whichever came first; a clean answer is "
            f"within {OPEN_CLEAN_S:.0f}s of the player's first packet on that hole; {OPEN_PROOF} clean "
            f"answers needed before a host is called open" if OPEN_STRICT else "as before (SCCT_RELAY_OPEN_STRICT=0)"))
        log(f"  HOLD UNPINGED: {HOLD_MODE} - on a host whose router blocks a hole that got relay packets before the host's ping "
            f"(no port forward), a joiner waits on a cold hole (host silent >{HOLD_COLD_S:.0f}s"
            + (f", >{HOLD_COLD_STRICT_S:.0f}s on a strict host" if HOLD_COLD_STRICT_S > 0 else "")
            + ") until the ping opens it" + (" (hosts are tested on their first such link)" if HOLD_MODE == "auto" else ""))
        if HOLD_MODE == "auto":
            log(f"  NO-REPLY VERDICT: " + (f"on - an untested host is judged strict when a link gets no reply on a hole "
                f"that was dead when it started (no game data >{HOLD_COLD_STRICT_S if HOLD_COLD_STRICT_S > 0 else HOLD_COLD_S:.0f}s, "
                f"no ping >{HOLD_PING_OPEN_S:.0f}s) while the host answers others" if HOLD_NOREPLY_STRICT else "off"))
        log(f"  SPARE POOL: " + (f"{POOL_SIZE} spare holes kept ready on hosts without a port forward (two-way after the host's "
            f"ping, keepalive every {POOL_KA_S:.0f}s; trusted up to a fixed {POOL_TRUST_S:.0f}s of silence, not grown from "
            f"evidence; " + (f"one miss steps trust down to {POOL_TRUST_STEP_S:.0f}s, a spare the host pinged that recently; "
                             f"a miss at that ceiling switches it off)" if POOL_STEPDOWN else
                             f"{POOL_MAX_MISSES} miss(es) switch it off for that host)") if POOL_SIZE > 0 else "off"))
        log(f"  REUSE QUIET: " + (f"on - another player's idle hole is reused only after the host is quiet on it {REUSE_OTHER_QUIET_S:.0f}s"
            if REUSE_OTHER_QUIET_S > 0 else "off"))
        log(f"  OWNER KEEP: " + (f"on - another player's link keeps its hole through a {OWNER_PAUSE_S:.0f}s pause" if OWNER_KEEP
            else "off"))
        log(f"  OWN SOCKET KEEP: " + (f"on - a player's socket that came back after >{OWN_RETURN_S:.0f}s before keeps its hole "
            f"(up to {OWN_SOCKET_KEEP_S:.0f}s quiet) against that player's other established sockets" if OWN_SOCKET_KEEP else "off"))
        log(f"  REBIND FOLLOW: " + (f"on - a player back from a new port {LINK_IDLE_S:.0f}-{REBIND_S:.0f}s after its newest link "
            f"(<= {REBIND_MAX_AGE_S:.0f}s old) paused keeps that link's hole" if REBIND_FOLLOW else "off"))
        log(f"  AGE LOG: " + (f"on (log only) - one 'AGE' line per link start or return on a hole: host silence there, cold "
            f"verdict, holds/moves, and the wait for the host's first packet (outcome=never after {AGE_TIMEOUT_S:.0f}s)"
            if AGE_LOG else "off"))
        log(f"  SLOW JOIN LOG: " + (f"on (log only) - 'SLOW JOIN' line when a new link has no host packet after "
            f"{SLOW_JOIN_WARN_S:.0f}s (the game gives up on a join at {GAME_JOIN_LIMIT_S:.0f}s)"
            if SLOW_JOIN_LOG and AGE_LOG else "on, but no lines (needs AGE LOG)" if SLOW_JOIN_LOG else "off"))
        log(f"  SHADOW HOLES: " + (f"on (log only) - evidence per kind of hole (pinged <= {HOLD_PING_OPEN_S:.0f}s, fresh = never or "
            f">= {SHADOW_FRESH_S:.0f}s, stale = in between) and a 'SHADOW link' line saying what a fresh-hole rule would do; "
            f"routing unchanged" if SHADOW and AGE_LOG else "on, but no SHADOW link lines (needs AGE LOG)" if SHADOW else "off"))
        log(f"  UPNP PROBE: " + (f"on (log only) - on request from the SCOPS window, test packets to the PC's UDP test port "
            f"from server ports {PROBE_PORTS[0]}-{PROBE_PORTS[-1]}: fresh, and stale {PROBE_STALE_S:.0f}s after a two-way exchange"
            if UPNP_PROBE and UPNP_HINT else "off"))
        log(f"  UPNP HINT: " + ("on - scopsDNS {\"op\":\"upnp_open\"} datagrams are logged and never relayed to a player"
            if UPNP_HINT else "off"))
        log(f"  FAIR SHADOW: " + (f"on (log only) - counts warm holes handed to another player's link and what that owner "
            f"then waited ('FAIR shadow' lines, summary every {FAIR_SUMMARY_S:.0f}s)" if FAIR_SHADOW else "off"))
        log(f"  UPNP OPEN SHADOW: " + (f"on (log only) - when a host is judged strict, says whether its SCOPS window reported a "
            f"working UPnP forward (last {UPNP_OPEN_HINT_S:.0f}s) and fresh router probes reached it ('UPNP_OPEN shadow' lines)"
            if UPNP_OPEN_SHADOW else "off"))
        log(f"  PAUSE AWARE: " + (f"on - a cold-hole strict verdict waits up to {PAUSE_CONFIRM_S:.0f}s for the host to send to "
            f"someone else; if it is silent everywhere (a pause) the verdict is cancelled (max {PAUSE_MAX_CANCELS} pauses per host)"
            if PAUSE_AWARE else "off"))
        log(f"  PAUSE SHADOW: " + (f"on (log only) - after a host is judged strict, watches {PAUSE_CHECK_S:.0f}s and says "
            f"whether the host went quiet on all its other holes for >={PAUSE_GAP_S:.0f}s (a pause, not a blocking router) "
            f"('PAUSE shadow' lines)" if PAUSE_SHADOW else "off"))
        log(f"  FAST RESUME: " + (f"on (log only) - flags a link quiet >{FAST_QUIET_S:.0f}s that the host answers in "
            f"<{FAST_WAIT_S:.1f}s during a game, and whether that player leaves the room within {FAST_WATCH_S:.0f}s "
            f"('FAST RESUME' lines)" if FAST_RESUME_LOG and AGE_LOG else "on, but no lines (needs AGE LOG)" if FAST_RESUME_LOG else "off"))
        log(f"  RELAY GUARD: " + (f"on (log only, nothing dropped) - per-link peak packets/s and largest packet, 'would-limit' "
            f"lines above {GUARD_PPS} pkt/s or {GUARD_MAX_PKT} B, summary every {GUARD_SUMMARY_S:.0f}s, unmapped senders counted"
            if GUARD_LOG else "off"))
        log(f"  FAIL STEPS: " + (f"on (log only) - NAT step lines (a socket stuck {NAT_STALL_S:.0f}s at PORT_ID), FAIL why on "
            f"no-reply moves, avoided holes on AGE lines, evidence on address-restricted" if FAIL_LOG else "off"))
        if WAKE_MOVE:
            log(f"  WAKE MOVE: on - a joiner socket back after >{WAKE_IDLE_S:.0f}s whose hole is cold (>{WAKE_COLD_S:.0f}s) moves to a hole the host used <{WAKE_WARM_S:.0f}s ago")
    elif RELAY_ALT_HOST_PORTS:
        log(f"  ALT-SOCKET mode: links delivered from 45000 to host sockets 3658 then {RELAY_ALT_HOST_PORTS} (external ports learned from each socket's pings); no steering")
    else:
        log("  RELAY: off")
    all_socks = list(socks) + list(relay_socks) + ([ctrl_sock] if ctrl_sock is not None else [])
    log(f"  LOOP STATS: " + (f"on (log only) - every {LOOP_STATS_S:.0f}s: time relayed game packets spend in the server, "
        f"housekeeping per pass, and in-loop pauses ('LOOP STATS' lines)" if LOOP_STATS else "off"))
    lstats = LoopStats(time.perf_counter()) if LOOP_STATS else None
    while True:
        r, _, _ = select.select(all_socks, [], [], 1.0 if RELAY_HOLES else 10.0)
        now = time.monotonic()
        if lstats is not None:
            p_wake = time.perf_counter()
        if RELAY_HOLES:
            hole_steering(now)
            hole_keepalive(now)
        if PROBE_RUNS:
            probe_tick(now)
        if CLIENTS and FAIL_LOG:
            nat_step_tick(now)
        for c in [c for c in CLIENTS if now - c.last_seen > CLIENT_TIMEOUT]:
            nat_step_waiting(c, now, "went silent (NAT session expired)")
            CLIENTS.remove(c)
            log(f"expired client {c.addr}:{c.port}")
            if not any(o.addr == c.addr for o in CLIENTS) and c.addr not in RELAY_HOSTS:
                for d in (HOST_TRAFFIC, HOST_EXT, HOST_EXT_LOCK, RELAY_TRAFFIC, KA_STATE, HOST_DATA, BAD_HOLES):
                    d.pop(c.addr, None)
        if GUARD_LOG:
            guard_tick(now)
        if FAIR_SHADOW:
            fair_tick(now)
        if FAST_RESUME_LOG:
            fast_resume_tick(now)
        if PAUSE_SHADOW:
            pause_shadow_tick(now)
        if RELAY_HOSTS:
            relay_expire(now)
            link_summary(now)
            link_silence(now)
            leave_expire(now)
            age_tick(now)
            if SHADOW:
                shadow_tick(now)
        if lstats is not None:
            lstats.housekeeping(time.perf_counter() - p_wake)
            lstats.tick(p_wake)
        for s in r:
            try:
                data, addr = s.recvfrom(2048)
            except OSError as e:
                log(f"recv error: {e}")
                continue
            if s is ctrl_sock:
                relay_control(data, addr)
                continue
            if s in relay_socks:
                try:
                    relay_from_joiner(s, data, addr, now)
                except Exception as e:
                    log(f"RELAY error from {addr[0]}:{addr[1]}: {e!r}")
                if lstats is not None:
                    lstats.packet(True, time.perf_counter() - p_wake)
                continue
            try:
                packets = handle_packet(data, addr, now, socks[s])
            except Exception as e:  # one bad packet must not stop the service
                import traceback
                log(f"bad packet from {addr[0]}:{addr[1]} ({len(data)}B): {e!r}")
                log(traceback.format_exc())
                continue
            for i, (pkt, dest) in enumerate(packets):
                if i and GAP_S > 0:
                    if lstats is not None:
                        g0 = time.perf_counter()
                        time.sleep(GAP_S)
                        g1 = time.perf_counter()
                        try:  # did packets arrive during the pause and wait for it? (peeks only; nothing is read)
                            q, _, _ = select.select(all_socks, [], [], 0)
                        except OSError:
                            q = []
                        lstats.pause(g1 - g0, bool(q), any(x in relay_socks for x in q))
                    else:
                        time.sleep(GAP_S)
                s.sendto(pkt, dest)
            if lstats is not None:
                lstats.packet(addr[0] in RELAY_HOSTS, time.perf_counter() - p_wake)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
