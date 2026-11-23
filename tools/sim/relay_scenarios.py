#!/usr/bin/env python3
"""Scenario replay for the relay's hole decisions (tools/nat2/udp_reply_server.py), old behaviour vs new.

Drives the real relay functions with a simulated clock, fake sockets, a host console (pings its steered hole every 25 s,
answers every packet it receives on the hole it arrived on, except while loading a map) and a network that can drop
packets per hole and direction. Each scenario follows a sequence seen in the logs of 2026-11-13.

    python relay_scenarios.py            (all scenarios, old and new)
    python relay_scenarios.py -v         (also print the relay's own log lines)
"""
from __future__ import annotations

import heapq
import importlib.util
import itertools
import re
import json
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
os.environ.setdefault("SCCT_RELAY_HOLES", ",".join(str(p) for p in range(45001, 45013)))
os.environ.setdefault("SCCT_RELAY_WARM_HOLES", "1")
os.environ["SCCT_RELAY_SNAPSHOT"] = ""
VERBOSE = "-v" in sys.argv

HOST, JOINER = "192.0.2.96", "198.51.100.205"
BASE = 1000.0


class Sim:
    def __init__(self):
        self.t = BASE
        self.q = []
        self.n = itertools.count()
        self.logs = []

    def at(self, t, fn, *a):
        heapq.heappush(self.q, (t, next(self.n), fn, a))

    def run_until(self, t_end, mod):
        next_tick = self.t
        while True:
            t_ev = self.q[0][0] if self.q else 1e18
            if next_tick <= t_ev and next_tick <= t_end:
                self.t = next_tick
                mod.hole_steering(self.t)
                mod.hole_keepalive(self.t)
                if mod.RELAY_HOSTS:
                    mod.link_silence(self.t)
                    mod.leave_expire(self.t)
                    if hasattr(mod, "age_tick"):
                        mod.age_tick(self.t)
                    if getattr(mod, "SHADOW", False):
                        mod.shadow_tick(self.t)        # same order as the relay's main loop
                next_tick += 1.0
                continue
            if t_ev > t_end:
                self.t = t_end
                return
            t, _, fn, a = heapq.heappop(self.q)
            self.t = t
            fn(*a)


SIM = None


def load_relay():
    path = HERE.parent / "nat2" / "udp_reply_server.py"
    spec = importlib.util.spec_from_file_location(f"relay_{next(_ids)}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ids = itertools.count()


class FakeSock:
    def __init__(self, port, on_send):
        self.port, self.on_send = port, on_send

    def getsockname(self):
        return ("0.0.0.0", self.port)

    def sendto(self, data, dest):
        self.on_send(self.port, data, dest)


class Clt:
    """Stand-in for the host console's NAT client (SRP session)."""
    def __init__(self):
        self.addr, self.port = HOST, 10070
        self.hole_msg = object()
        self.hole_target, self.hole_last, self.hole_count = None, 0.0, 0
        self.last_seen = 0.0
        self.reqs = {}
        self.warm_prev = False


class World:
    """One scenario run: relay module + host console + network + joiner endpoints."""
    def __init__(self, new: bool, block_in=(), block_out=(), nat=False, poison=False, pool=None, out_only=False,
                 used_life=180.0, upnp=None):
        global SIM
        SIM = self.sim = Sim()
        self.mod = mod = load_relay()
        mod.time.monotonic = lambda: SIM.t
        mod.log = lambda m: SIM.logs.append((SIM.t, m))
        for flag in ("LEAVE_GRACE", "NOREPLY_MOVE", "SRP_BY_SESSION"):
            setattr(mod, flag, new)
        mod.HOLD_MODE = "auto" if new else "off"
        mod.POOL_SIZE = (3 if new else 0) if pool is None else pool      # run_public_test.bat ships with 3
        # out_only: a router that refreshes a hole only on the HOST's packets and never gives it the long timeout
        # (keepalives from the relay cannot hold it open): the spare pool must notice and switch itself off
        self.out_only = out_only
        self.used_life = used_life   # poison model: how long a hole that carried traffic both ways outlives its last packet
        # upnp (poison model only): the host's router has a port forward (COOLDOR's ASUS with UPnP, 2026-11-16). A hole that
        # never had an entry, or whose entry expired more than `upnp` seconds ago, lets relay packets in through the forward;
        # one whose entry expired more recently drops them (hole 45001 at 05:38:50: 17 s of DORIAN_D's packets lost while
        # fresh holes 45002/45004 got through).
        self.upnp = upnp
        self.deliveries = []        # (time, hole, delivered?) for every joiner packet that reaches the host's router
        self.seen_reply = {}
        # poison: router with no port forward (2026-11-14 22:08): a relay packet that reaches a hole the host has not
        # pinged leaves a NAT entry that blocks the host's next ping there until 30 s after the last such packet
        self.poison = poison
        self.poisoned = {}
        # poison model only: a hole's NAT entry expires. One the host has only pinged lasts 30 s after the last ping (no
        # reply ever comes back on that flow); once relay packets got in and the host answered, it lasts 180 s.
        self.open_until = {}
        self.assured = set()
        nat = nat or poison
        if not new:
            mod.REUSE_OTHER_QUIET_S = 0
        mod.nat_reply = lambda msg, kind, a, port, pick_id=False: (None, next(_ids) + 0x1000)
        mod.srp.SRPResponse = lambda *a: b""
        self.block_in, self.block_out = set(block_in), set(block_out)
        mod.HOLE_SOCKS.update({p: FakeSock(p, self._relay_to_host) for p in mod.RELAY_HOLES})
        mod.NAT_MAIN_SOCK = FakeSock(45000, self._steer_sent)
        self.jsock = FakeSock(3658, self._relay_to_joiner)
        self.clt = Clt()
        mod.CLIENTS.append(self.clt)
        self.loading = False
        self.nat = nat             # host behind a filtering NAT: only holes it has pinged let relay packets in
        self.opened = set()
        self.host_peers = {}       # hole -> number of joiner packets the host received from relay:hole
        self.got = {}              # joiner endpoint port -> host packets received
        self.last_reply = {}       # joiner endpoint port -> time of the last host packet it received
        self.first_reply = {}      # joiner endpoint port -> time of the first host packet it received
        self.reply_times = {}      # joiner endpoint port -> times of host packets (at most one per second)
        self.ep_hole = {}          # joiner endpoint port -> hole of the last packet it sent through the relay
        self.moves_after_answer = []   # (t, port, old, new): a hole change for a link the host had answered
        self.answered = set()
        self._ping_loop()

    # --- host console -------------------------------------------------------------------------------------------
    def is_open(self, hole):
        if not self.poison:
            return hole in self.opened
        return SIM.t < self.open_until.get(hole, -1e9)

    def _forward_ok(self, hole):
        if self.upnp is None or not self.poison:
            return False
        last = self.open_until.get(hole)
        return last is None or SIM.t - last >= self.upnp

    def _refresh(self, hole, inbound):
        """A packet passes the host's router on this hole. Like Linux conntrack: the first packet coming back in marks the
        entry as seen-reply; a later packet (2 s on) makes it assured, and only then does it get the long timeout."""
        t = SIM.t
        if not self.is_open(hole):
            self.assured.discard(hole)
            self.seen_reply.pop(hole, None)
        if inbound:
            if self.out_only:
                return
            self.seen_reply.setdefault(hole, t)
        if hole in self.seen_reply and t - self.seen_reply[hole] >= 2.0:
            self.assured.add(hole)
        self.open_until[hole] = max(self.open_until.get(hole, -1e9), t + (self.used_life if hole in self.assured else 30.0))

    def _ping_loop(self):
        t = SIM.t
        self.clt.last_seen = t                   # sockets 2-4 ping 45000 on the same cycle
        port = self.clt.hole_target
        if port is not None and not self.is_open(port) and t - self.poisoned.get(port, -1e9) < 30.0:
            self.lost_pings = getattr(self, "lost_pings", 0) + 1
            port = None                          # blocked by the entry the relay's early packets left: ping lost
        if port is not None and port not in self.block_out:
            self.opened.add(port)                # the ping opens the host's NAT for that hole either way
            if self.poison:
                self._refresh(port, False)
            if self.clt in self.mod.CLIENTS:     # ...but only a known session's ping is seen (and answered) by the relay
                self.mod.HOST_TRAFFIC.setdefault(HOST, {})[port] = t
                self.mod.HOST_EXT.setdefault(HOST, {})[port] = 3658
        SIM.at(t + 25.0, self._ping_loop)

    def _steer_sent(self, port, data, dest):
        pass                                     # SetPingPort: the next ping goes to clt.hole_target

    def _relay_to_host(self, hole, data, dest):
        if hole in self.block_in:
            return
        if self.nat and not self.is_open(hole) and not self._forward_ok(hole):
            if len(data) > 2:
                self.deliveries.append((SIM.t, hole, False))
            if self.poison:
                self.poisoned[hole] = SIM.t
            return                               # dropped by the host's router
        if len(data) > 2:
            self.deliveries.append((SIM.t, hole, True))
        if self.poison:
            self._refresh(hole, True)            # a reply on the host's own flow: entry now carries traffic both ways
        if len(data) <= 2:
            return                               # keepalive
        self.host_peers[hole] = self.host_peers.get(hole, 0) + 1
        if not self.loading:
            SIM.at(SIM.t + 0.03, self._host_reply, hole)

    def _host_reply(self, hole):
        if hole in self.block_out:
            return
        if self.poison:
            self._refresh(hole, False)
        self.mod.handle_packet(b"\x05\x00reply", (HOST, 3658), SIM.t, hole)

    def _relay_to_joiner(self, port, data, dest):
        self.got[dest[1]] = self.got.get(dest[1], 0) + 1
        self.last_reply[dest[1]] = SIM.t
        self.first_reply.setdefault(dest[1], SIM.t)
        rt = self.reply_times.setdefault(dest[1], [])
        if not rt or SIM.t - rt[-1] >= 1.0:
            rt.append(SIM.t)
        self.answered.add(dest[1])

    # --- joiner ---------------------------------------------------------------------------------------------------
    def send(self, port, t0, t1, rate=5.0, ip=JOINER):
        """The joiner's socket `port` sends `rate` packets/s from t0 to t1."""
        n = int((t1 - t0) * rate)
        for i in range(n + 1):
            SIM.at(BASE + t0 + i / rate, self._joiner_pkt, port, ip)

    def _joiner_pkt(self, port, ip=JOINER):
        mod = self.mod
        mod.relay_from_joiner(self.jsock, b"\x07\x00joiner", (ip, port), SIM.t)
        hv = mod.RELAY_HOSTS.get(HOST)
        hole = hv["src"].get((ip, port)) if hv else None
        old = self.ep_hole.get(port)
        if old is not None and hole != old and port in self.answered and old not in mod.BAD_HOLES.get(HOST, {}):
            self.moves_after_answer.append((SIM.t, port, old, hole))
        if hole != old:
            self.answered.discard(port) if old is not None else None
        self.ep_hole[port] = hole

    def ctrl(self, t, **m):
        SIM.at(BASE + t, lambda: self.mod.relay_control(json.dumps(m).encode(), ("127.0.0.1", 1)))

    def host_load(self, t0, t1):
        SIM.at(BASE + t0, setattr, self, "loading", True)
        SIM.at(BASE + t1, setattr, self, "loading", False)

    def hole_of(self, port):
        hv = self.mod.RELAY_HOSTS.get(HOST)
        return hv["src"].get((JOINER, port)) if hv else None

    def lines(self, *keys):
        return [f"{t - BASE:7.1f}s {m}" for t, m in SIM.logs if any(k in m for k in keys)]


def setup_room(w: World, t=0.0):
    """Host logged in and pinging; joiner joined (probe + game link, as every successful join tonight)."""
    w.ctrl(t + 30, op="map", joiner=JOINER, host=HOST, room="103", user="DORIAN_D")
    w.send(10072, t + 30.2, t + 30.6)      # join probe
    w.send(10071, t + 31.0, t + 60)        # game link (room lobby heartbeats)


def leave(w: World, t):
    w.ctrl(t, op="reset_joiner", joiner=JOINER, why="GROUP_LEAVE room 103", user="DORIAN_D")


def rejoin(w: World, t, probe, link, until):
    w.ctrl(t, op="map", joiner=JOINER, host=HOST, room="103", user="DORIAN_D")
    w.send(probe, t + 0.4, t + 0.8)
    w.send(link, t + 1.3, until)


# --------------------------------------------------------------------------------------------------------------------
def sc_leave_freeze(new):
    """05:29:16 - the PS2 leaves while its link is still sending; old code moved that live link to another hole."""
    w = World(new)
    setup_room(w)
    w.send(10071, 60, 120)
    leave(w, 120.0)
    w.send(10071, 120.1, 121.0)            # heartbeats keep going for a moment after the leave
    w.sim.run_until(BASE + 130, w.mod)
    ok = not w.moves_after_answer
    return ok, (f"live link kept its hole through the leave" if ok else
                f"live link moved {w.moves_after_answer[0][2]} -> {w.moves_after_answer[0][3]} right after the leave "
                f"(host sees the player from a second address: the freeze)")


def sc_leave_then_rejoin(new):
    """05:28:25 leave with the link still sending, 05:28:47 rejoin: the probe went out on the stale mapping and the join
    link got another hole (split: join never completed)."""
    w = World(new)
    setup_room(w)
    w.send(10073, 60, 125)                 # the round's game link on another socket
    leave(w, 125.0)
    w.send(10073, 125.1, 128.0)            # goodbye packets after the leave
    rejoin(w, 147.0, probe=10073, link=10072, until=170)
    w.sim.run_until(BASE + 175, w.mod)
    a, b = w.hole_of(10073), w.hole_of(10072)
    ok = a == b and w.got.get(10072, 0) > 0
    return ok, (f"probe and join link share hole {a}; host answering" if ok else
                f"join split: probe on {a}, join link on {b} (join does not complete)")


def sc_inbound_block(new):
    """04:30 - the host's router drops relay packets from the ping-target hole; the reconnect lands there."""
    w = World(new)
    setup_room(w)
    w.sim.run_until(BASE + 70, w.mod)
    tgt = w.clt.hole_target
    w.block_in.add(tgt)
    w.send(10071, 70, 140)                 # lobby link keeps working
    w.send(10073, 75, 140)                 # back to the lobby: new connection, lands on the ready hole
    w.sim.run_until(BASE + 140, w.mod)
    got = w.got.get(10073, 0)
    ok = got > 0
    first = next((t for t, m in w.sim.logs if "no reply on hole" in m), None)
    return ok, (f"blocked hole {tgt}: link moved after {first - BASE - 75:.0f}s, host answering ({got} replies)" if ok
                else f"blocked hole {tgt}: link never answered in 65 s (player gives up)")


def sc_outbound_block(new):
    """04:04 - the host's packets to one hole stop arriving; a reconnect on that hole is never answered."""
    w = World(new)
    setup_room(w)
    w.send(10073, 60, 200)                  # second link, host answering
    w.sim.run_until(BASE + 70, w.mod)
    dead = w.hole_of(10071)
    w.block_out.add(dead)
    w.send(10071, 90, 160)                  # 10071 reconnects on its old hole after a load: the host's replies are lost
    w.sim.run_until(BASE + 89, w.mod)
    before = w.got.get(10071, 0)
    w.sim.run_until(BASE + 160, w.mod)
    got = w.got.get(10071, 0) - before
    ok = got > 0
    first = next((t for t, m in w.sim.logs if "no reply on hole" in m), None)
    return ok, (f"dead hole {dead}: reconnect moved after {first - BASE - 90:.0f}s, host answering" if ok else
                f"dead hole {dead}: reconnect never answered in 70 s (player gets 'game no longer available')")


def sc_failed_steer_rejoin(new):
    """04:05-04:07 - a steer whose ping never arrives; the rejoins were sent to that hole and all failed."""
    w = World(new, block_out={45003})
    setup_room(w)                           # probe + link on 45001, spare steered to 45002
    w.send(10071, 60, 158)
    w.send(10073, 70, 150)                  # second link takes 45002; the next spare steer goes to 45003 (blocked)
    leave(w, 160.0)
    rejoin(w, 175.0, probe=10072, link=10071, until=205)
    w.sim.run_until(BASE + 175, w.mod)
    before = w.got.get(10071, 0)
    w.sim.run_until(BASE + 210, w.mod)
    hp, h = w.hole_of(10072), w.hole_of(10071)
    bad = "HOLE bad" in " ".join(m for _, m in w.sim.logs)
    ok = w.got.get(10071, 0) > before and hp == h != 45003
    return ok, (f"steer to 45003 marked failed; rejoin on one hole ({h}), host answering" if ok else
                f"rejoin probe on {hp}, link on {h} (45003's steer never got a ping)" + ("" if bad else "; failure not noticed"))


def sc_no_false_move(new):
    """Normal revenge reload: host silent everywhere for 8 s while two reconnects wait, then answers them 0.5 s apart."""
    w = World(new)
    setup_room(w)
    w.send(10073, 60, 100)
    w.host_load(100, 108)
    w.send(10072, 101, 140)
    w.send(10073, 101.5, 140)
    w.sim.run_until(BASE + 140, w.mod)
    moves = w.lines("no reply on hole")
    ok = not moves and w.got.get(10072, 0) > 0
    return ok, ("no link moved during the load; both answered after it" if ok else f"false move: {moves[:1]}")


def sc_join_split_steer(new):
    """Normal fresh join with the ping target not yet confirmed: still lands on one hole."""
    w = World(new)
    setup_room(w)
    w.sim.run_until(BASE + 65, w.mod)
    ok = w.hole_of(10071) == w.hole_of(10072) and w.got.get(10071, 0) > 0
    return ok, f"first join: probe {w.hole_of(10072)}, link {w.hole_of(10071)}"


def sc_remapped_srp_third_joiner(new):
    """23:21-23:33 and 00:20 - host behind a port-rewriting router (SRP session on 4465, not 10070): the first joiner's
    arrival dropped the host's session as "stray", steering died, and every third joiner failed on never-opened 45003."""
    w = World(new, nat=True)
    w.clt.port = 4465                       # Snake's router moved the SRP session off 10070
    w.clt.reqs = {0x11111111: {"sock": 1, "pending": False}}
    ja, jb, jc = "203.0.113.24", "198.51.100.205", "198.18.133.242"
    w.ctrl(30, op="map", joiner=ja, host=HOST, room="104", user="StandaloneF0X")
    w.send(3576, 30.2, 200, ip=ja)
    SIM.at(BASE + 31.5, lambda: w.mod.handle_packet(bytes(12), (HOST, 4465), SIM.t, 45000))   # host's SRP ack, just after steer #2 (as at 23:21:58)
    w.ctrl(60, op="map", joiner=jb, host=HOST, room="104", user="DORIAN_D")
    w.send(10071, 60.2, 200, ip=jb)
    w.ctrl(100, op="map", joiner=jc, host=HOST, room="104", user="Wickedness")
    tries = [(62937, 100), (62938, 113), (62993, 126), (62994, 139), (62995, 152)]
    for port, t in tries:                   # each try: ~5 s of join packets, then "game no longer available"
        w.send(port, t + 0.2, t + 5.2, ip=jc)
    w.sim.run_until(BASE + 200, w.mod)
    ok_ab = w.got.get(3576, 0) > 0 and w.got.get(10071, 0) > 0
    won = next((i + 1 for i, (p, _) in enumerate(tries) if w.got.get(p, 0) > 0), None)
    kept = w.clt in w.mod.CLIENTS
    ok = ok_ab and won is not None and kept
    return ok, (f"host session kept; joiners 1 and 2 in; third joiner in on try {won}" if ok else
                f"host session {'kept' if kept else 'dropped as stray'}; joiners 1/2 {'in' if ok_ab else 'NOT in'}; "
                f"third joiner never answered in {len(tries)} tries")


def sc_fast_third_joiner(new):
    """Same host, all three join within seconds (one ping cycle opens one hole): the third retries until its hole is open,
    and a hole the host was only just steered to is not marked bad while the steer is pending."""
    w = World(new, nat=True)
    w.clt.port = 4465
    w.clt.reqs = {0x11111111: {"sock": 1, "pending": False}}
    ja, jb, jc = "203.0.113.24", "198.51.100.205", "198.18.133.242"
    w.ctrl(30, op="map", joiner=ja, host=HOST, room="104", user="StandaloneF0X")
    w.send(3576, 30.2, 200, ip=ja)
    SIM.at(BASE + 31.5, lambda: w.mod.handle_packet(bytes(12), (HOST, 4465), SIM.t, 45000))
    w.ctrl(35, op="map", joiner=jb, host=HOST, room="104", user="DORIAN_D")
    w.send(10071, 35.2, 200, ip=jb)
    tries = [(60000 + i, 38 + 13 * i) for i in range(6)]
    for port, t in tries:                   # each try is a new JOIN_ROOM
        w.ctrl(t, op="map", joiner=jc, host=HOST, room="104", user="Wickedness")
        w.send(port, t + 0.2, t + 5.2, ip=jc)
    w.sim.run_until(BASE + 200, w.mod)
    won = next((i + 1 for i, (p, _) in enumerate(tries) if w.got.get(p, 0) > 0), None)
    bad = w.lines("HOLE bad")
    ok = w.got.get(10071, 0) > 0 and won is not None and not bad
    return ok, (f"second joiner in; third in on try {won} ({tries[won - 1][1] - 35:.0f}s after the second); no hole marked bad"
                if ok else f"second {'in' if w.got.get(10071, 0) else 'NOT in'}; third {'try ' + str(won) if won else 'never in'}; "
                f"{len(bad)} hole(s) marked bad")


def sc_reuse_other_live_hole(new):
    """00:19:50 - a rejoin took another player's previous-map hole (idle on the player's side, but the host still sending
    on it 6 s before): the host saw the newcomer as that player's old connection and the join failed."""
    w = World(True)                          # tonight's relay; "old" = only the reuse guard off
    if not new:
        w.mod.REUSE_OTHER_QUIET_S = 0
    other, me = "198.18.133.242", "198.18.103.130"
    w.ctrl(30, op="map", joiner=other, host=HOST, room="100", user="Wickedness")
    w.send(63550, 30.2, 60, ip=other)        # Wickedness's previous-map link: player side goes quiet at 60 s
    for i in range(60):                      # ...but the host keeps sending to it for a while (its own timeout)
        SIM.at(BASE + 60 + i * 0.2, lambda: w.mod.handle_packet(b"\x05\x00reply", (HOST, 3658), SIM.t, 45001))
    w.ctrl(50, op="map", joiner="203.0.113.24", host=HOST, room="100", user="StandaloneF0X")
    w.send(3654, 50.2, 75, ip="203.0.113.24")   # Fox's new link takes the ready hole; the next spare is not pinged yet
    w.ctrl(68, op="map", joiner=me, host=HOST, room="100", user="SNAKE")
    w.send(10071, 68.2, 73.2, ip=me)
    w.sim.run_until(BASE + 75, w.mod)
    hv = w.mod.RELAY_HOSTS[HOST]
    mine = hv["src"].get((me, 10071))
    theirs = 45001
    ok = mine is not None and mine != theirs
    return ok, (f"rejoin got hole {mine}, not the other player's still-live hole {theirs}" if ok else
                f"rejoin put on the other player's hole {theirs} while the host still sends there (join fails)")


class Player:
    """A joiner that retries like the game: each try is a JOIN_ROOM on a fresh socket, ~5 s of join packets, then either
    it is in (keeps sending) or it leaves (GROUP_LEAVE) and tries again `gap` s after the try started."""
    def __init__(self, w: World, ip: str, user: str, room: str, base_port: int, gap: float = 11.0, max_tries: int = 8):
        self.w, self.ip, self.user, self.room = w, ip, user, room
        self.next_port, self.gap, self.max_tries = base_port, gap, max_tries
        self.port, self.gen = None, 0
        self.joins = []          # [start time, tries, joined?]
        self.reconnects = []     # (time, port) of each reconnect
        self.host_lan = ""       # what the lobby server passes as the host's self-reported address

    def _ctrl(self, op):
        m = {"op": op, "joiner": self.ip, "host": HOST, "room": self.room, "user": self.user}
        if op == "map" and self.host_lan:
            m["host_lan"] = self.host_lan
        if op == "reset_joiner":
            m["why"] = f"GROUP_LEAVE room {self.room}"
        self.w.mod.relay_control(json.dumps(m).encode(), ("127.0.0.1", 1))

    def _loop(self, port, gen):
        if gen != self.gen or port != self.port:
            return
        self.w._joiner_pkt(port, self.ip)
        SIM.at(SIM.t + 0.2, self._loop, port, gen)

    def join(self, t):
        SIM.at(BASE + t, self._start)

    def leave(self, t):
        SIM.at(BASE + t, self._leave)

    def _start(self):
        self.joins.append([SIM.t, 0, False])
        self._try()

    def _try(self):
        j = self.joins[-1]
        j[1] += 1
        self.port, self.gen = self.next_port, self.gen + 1
        self.next_port += 1
        self._ctrl("map")
        SIM.at(SIM.t + 0.2, self._loop, self.port, self.gen)
        SIM.at(SIM.t + 5.3, self._check, self.port, SIM.t)

    def _check(self, port, t_try):
        if port != self.port:
            return
        if self.w.got.get(port, 0) > 0:
            self.joins[-1][2] = True
            return
        self._ctrl("reset_joiner")                 # "game no longer available": back to the games list
        self.port, self.gen = None, self.gen + 1
        if self.joins[-1][1] < self.max_tries:
            SIM.at(t_try + self.gap, self._try)

    def reconnect(self, t):
        """Map load: the game drops its link and comes back on a new socket (no JOIN_ROOM)."""
        SIM.at(BASE + t, self._reconnect)

    def _reconnect(self):
        self.port, self.gen = self.next_port, self.gen + 1
        self.next_port += 1
        self.reconnects.append((SIM.t, self.port))
        SIM.at(SIM.t + 0.01, self._loop, self.port, self.gen)

    def game_link(self, t0, t1, port):
        """Match start: a SECOND link on another socket (the room link stays up), sending from t0 to t1. Counted in
        waits() like a reconnect."""
        def loop():
            if SIM.t >= BASE + t1:
                return
            self.w._joiner_pkt(port, self.ip)
            SIM.at(SIM.t + 0.2, loop)
        def start():
            self.w.first_reply.pop(port, None)
            self.reconnects.append((SIM.t, port))
            self._fr = getattr(self, "_fr", {})
            loop()
        SIM.at(BASE + t0, start)

    def pause(self, t):
        SIM.at(BASE + t, self._pause)

    def _pause(self):
        self.gen += 1                              # stops the loop; port kept so _loop resumes nothing

    def waits(self):
        """Seconds each reconnect waited for its first host packet (None = never)."""
        out = []
        for t, p in self.reconnects:
            ts = [x for x in self.w.reply_times.get(p, ()) if x >= t]
            nxt = min((t2 for t2, p2 in self.reconnects if p2 == p and t2 > t), default=1e18)
            out.append(ts[0] - t if ts and ts[0] < nxt else None)
        return out

    def _leave(self):
        self._ctrl("reset_joiner")
        self.port, self.gen = None, self.gen + 1

    def answered_since(self, t):
        """The current link got host replies after time t (it is still being answered)."""
        return self.port is not None and self.w.last_reply.get(self.port, -1e9) >= BASE + t


ANDROID, DORIAN = "198.19.103.62", "198.51.100.205"


def _joins_txt(p: Player):
    return ", ".join(f"{'in' if ok else 'NOT in'} after {n} tr{'y' if n == 1 else 'ies'}" for _, n, ok in p.joins)


def sc_strict_nat_second_joiner(new):
    """2026-11-14 22:08 - COOLDOR hosts with his port forward off; ANDROID-DOR joins, DORIAN_D 1 s later: every hole the
    relay sent DORIAN_D's packets into before the host's ping never got that ping (he only got in once ANDROID-DOR left)."""
    w = World(new, poison=True)
    a = Player(w, ANDROID, "ANDROID-DOR", "111", 62000)
    b = Player(w, DORIAN, "DORIAN_D", "111", 20000)
    a.join(30.0)
    b.join(31.0)
    w.sim.run_until(BASE + 200, w.mod)
    ok = bool(b.joins and b.joins[0][2] and a.joins[0][2] and a.answered_since(190))
    tries = b.joins[0][1] if b.joins else 0
    return ok, (f"ANDROID-DOR in on try 1; DORIAN_D in on try {tries} while ANDROID-DOR stays connected"
                + f"; {getattr(w, 'lost_pings', 0)} host ping(s) blocked" if ok else
                f"ANDROID-DOR {_joins_txt(a)}; DORIAN_D {_joins_txt(b)} while ANDROID-DOR is in; "
                f"{getattr(w, 'lost_pings', 0)} host pings blocked by early relay packets")


def sc_strict_nat_leave_rejoin_stress(new):
    """Same host (no forward): both joiners leave and rejoin over and over, as in tonight's stress test."""
    w = World(new, poison=True)
    a = Player(w, ANDROID, "ANDROID-DOR", "111", 62000)
    b = Player(w, DORIAN, "DORIAN_D", "111", 20000)
    a.join(30.0)
    b.join(31.0)
    for k in range(4):
        t = 130.0 + 60.0 * k
        a.leave(t); a.join(t + 3.0)
        b.leave(t + 25.0); b.join(t + 28.0)
    w.sim.run_until(BASE + 420, w.mod)
    allj = a.joins + b.joins
    ok = all(j[2] for j in allj)
    worst = max(j[1] for j in allj)
    return ok, (f"all {len(allj)} joins/rejoins got in (worst: {worst} tries)" if ok else
                f"ANDROID-DOR: {_joins_txt(a)}; DORIAN_D: {_joins_txt(b)}")


def sc_open_host_not_held(new):
    """Control: a host with a port forward (or a public IP, like DORIAN_D's PS2 on the modem) answers on any hole at once;
    two joiners 1 s apart must both get in on their first try - the hold must not slow such a host down."""
    w = World(new)
    a = Player(w, ANDROID, "ANDROID-DOR", "111", 62000)
    b = Player(w, "192.0.2.97", "COOLDOR", "111", 20000)
    a.join(30.0)
    b.join(31.0)
    w.sim.run_until(BASE + 120, w.mod)
    held = w.lines("holding ")
    state = w.mod.hold_state(HOST)
    ok = a.joins[0][2] and b.joins[0][2] and b.joins[0][1] == 1 and not held
    return ok, (f"both in on try 1; host judged '{state}', nothing held" if ok else
                f"ANDROID-DOR {_joins_txt(a)}; second {_joins_txt(b)}; host judged '{state}'; {len(held)} hold(s)")


def sc_strict_held_link_follows_ping(new):
    """2026-11-14 23:01:40 - both players in the room open a new link at the same moment; the second lands on a just-steered
    hole, is the probe that shows the host is strict, and (patch v1) stayed held on that blocked hole for 59 s."""
    w = World(new, poison=True)
    a = Player(w, ANDROID, "ANDROID-DOR", "100", 62000)
    b = Player(w, DORIAN, "DORIAN_D", "100", 20000)
    a.join(30.0)
    b.join(60.0)
    b.reconnect(150.0)
    a.reconnect(150.03)
    w.sim.run_until(BASE + 260, w.mod)
    wa, wb = a.waits()[0], b.waits()[0]
    ok = b.joins[0][2] and wa is not None and wa <= 35 and wb is not None
    f = lambda x: "never" if x is None else f"{x:.0f}s"
    return ok, (f"both new links answered: DORIAN_D after {f(wb)}, ANDROID-DOR after {f(wa)}" if ok else
                f"DORIAN_D joined={b.joins[0][2]}; new links: DORIAN_D {f(wb)}, ANDROID-DOR {f(wa)}")


def sc_strict_map_load_reconnects(new):
    """2026-11-14 23:07-23:16 - five matches; at every map load both players come back on new sockets. Reconnects that were
    sent into spares pinged 36-58 s before (gone on this NAT) waited ~18 s for the no-reply move."""
    w = World(new, poison=True)
    a = Player(w, ANDROID, "ANDROID-DOR", "101", 62000)
    b = Player(w, DORIAN, "DORIAN_D", "101", 20000)
    a.join(30.0)
    b.join(60.0)
    for k in range(5):
        t = 160.0 + 75.0 * k                   # match end / start: links stop, host loads, everyone reconnects
        a.pause(t); b.pause(t)
        w.host_load(t, t + 8.0)
        b.reconnect(t + 9.0)
        a.reconnect(t + 9.4)
    w.sim.run_until(BASE + 560, w.mod)
    ws = [x for x in a.waits() + b.waits()]
    never = sum(1 for x in ws if x is None)
    worst = max((x for x in ws if x is not None), default=None)
    slow = sum(1 for x in ws if x is not None and x > 5)
    ok = b.joins[0][2] and never == 0 and worst is not None and worst <= 30
    return ok, (f"all {len(ws)} reconnects answered; {slow} waited >5 s, worst {worst:.0f}s" if ok else
                f"{len(ws)} reconnects: {never} never answered, {slow} waited >5 s" +
                (f", worst {worst:.0f}s" if worst is not None else ""))


def sc_open_host_slow_reconnect(new):
    """2026-11-15 00:41:23 - DORIAN_D's PS2 (public IP, no router) did not answer SNAKE's reconnect after a map for 4 s
    while still sending to his other socket; patch v2 judged that host strict and later held StandaloneF0X's join."""
    w = World(new)
    a = Player(w, "198.18.103.130", "SNAKE", "102", 20000)
    a.join(30.0)
    st = {}
    def slow():                                  # host answers only the player's current hole for 6 s
        hv = w.mod.RELAY_HOSTS.get(HOST, {})
        cur = hv.get("src", {}).get(("198.18.103.130", a.port))
        w.block_in = set(p for p in w.mod.RELAY_HOLES if p != cur)
        st["cur"] = cur
    def fast():
        w.block_in = set()
    SIM.at(BASE + 259.8, slow)
    a.reconnect(260.0)
    SIM.at(BASE + 266.0, fast)
    f = Player(w, "203.0.113.24", "StandaloneF0X", "102", 30000)
    f.join(300.0)
    w.sim.run_until(BASE + 360, w.mod)
    state = w.mod.hold_state(HOST)
    held = w.lines("holding ")
    ok = a.joins[0][2] and f.joins and f.joins[0][2] and f.joins[0][1] == 1 and state != "strict"
    wt = a.waits()[0]
    return ok, (f"host judged '{state}'; reconnect answered after {wt:.0f}s; late joiner in on try 1; {len(held)} hold(s)"
                if ok else f"host judged '{state}'; late joiner {_joins_txt(f)}; {len(held)} hold(s)")


def sc_public_ip_host_open(new):
    """A host whose console reports its public address as its own (no router) is open from its first join: even a
    first join probe it is slow to answer must not get it held."""
    w = World(new)
    w.block_in = set(w.mod.RELAY_HOLES)          # host slow on every new hole for the first 6 s
    SIM.at(BASE + 36.0, lambda: setattr(w, "block_in", set()))
    a = Player(w, ANDROID, "ANDROID-DOR", "103", 62000)
    a.host_lan = HOST
    a.join(30.0)
    w.sim.run_until(BASE + 120, w.mod)
    state = w.mod.hold_state(HOST)
    held = w.lines("holding ")
    ok = state == "open" and not held and a.joins[0][2]
    return ok, (f"host judged 'open' from its login address; joiner {_joins_txt(a)}; nothing held" if ok else
                f"host judged '{state}'; joiner {_joins_txt(a)}; {len(held)} hold(s)")


THIRD = "203.0.113.24"


def _pool_room(new, every, n, **kw):
    """Three joiners in a room; each keeps its room link up and opens a game link on another socket at every match start
    (two of them rotate three sockets like the PS2, the third has a router that gives every link a new port), all three
    within half a second, as in the logs. The match lasts `every` - 40 s."""
    w = World(True, poison=True, pool=1 if new else 0, **kw)   # "old" = the v3 relay without the pool; tightened: 1 spare
    ps = [Player(w, ANDROID, "ANDROID-DOR", "105", 62000), Player(w, DORIAN, "DORIAN_D", "105", 20000),
          Player(w, THIRD, "Wickedness", "105", 30000)]
    ps[0].join(30.0); ps[1].join(60.0); ps[2].join(61.0)     # two joins 1 s apart: where a strict host shows itself
    for k in range(n):
        t = 300.0 + every * k
        w.host_load(t - 4.0, t - 0.5)
        for i, p in enumerate(ps):
            port = 40000 + 100 * i + (k if i == 2 else k % 3)
            p.game_link(t + 0.2 * i, t + every - 40.0, port)
    w.sim.run_until(BASE + 300.0 + every * n, w.mod)
    ws = [x for p in ps for x in p.waits()]
    return w, ps, ws


def _waits_txt(ws):
    never = sum(1 for x in ws if x is None)
    got = [x for x in ws if x is not None]
    return (f"{len(ws)} reconnects: {sum(1 for x in got if x <= 2)} at once, {sum(1 for x in got if 2 < x <= 30)} within 30 s, "
            f"{sum(1 for x in got if x > 30)} later, {never} never" + (f"; worst {max(got):.0f}s" if got else ""))


def sc_strict_pool_map_change_4p(new):
    """2026-11-15 01:13-02:04 - 4-player room on a host with no forward: at each map change three joiners reconnect at
    once; the host opens one hole per 25 s, the third waited 53-59 s and the round started without it.
    2026-11-15 real test (COOLDOR, no forward, UPnP off): the old growing-trust pool (3 spares) backfired - see the
    note above POOL_SIZE in udp_reply_server.py. Tightened to 1 spare with a fixed, modest ceiling. One spare can
    help at most ONE of three simultaneous joiners at a map change, not all three, so this is not expected to beat
    the no-pool baseline; it only has to be no worse, and leave no bad holes behind."""
    w, ps, ws = _pool_room(new, 150.0, 6)
    w0, ps0, ws0 = _pool_room(False, 150.0, 6)
    never = sum(1 for x in ws if x is None)
    never0 = sum(1 for x in ws0 if x is None)
    ok = all(p.joins[0][2] for p in ps) and never <= never0 and not w.lines("HOLE bad")
    return ok, _waits_txt(ws) + (" (1 spare: no worse than no pool, not expected to beat it)" if new else "")


def sc_strict_pool_long_matches(new):
    """The same room with 10-minute matches: the spares must still be open when the map finally changes.
    Same honest bar as sc_strict_pool_map_change_4p: 1 spare, no worse than the no-pool baseline."""
    w, ps, ws = _pool_room(new, 610.0, 2)
    w0, ps0, ws0 = _pool_room(False, 610.0, 2)
    never = sum(1 for x in ws if x is None)
    never0 = sum(1 for x in ws0 if x is None)
    ok = all(p.joins[0][2] for p in ps) and never <= never0 and not w.lines("HOLE bad")
    return ok, _waits_txt(ws) + (" (1 spare: no worse than no pool, not expected to beat it)" if new else "")


def sc_strict_pool_router_ignores_keepalives(new):
    """Safety: a router that drops a hole 30 s after the HOST's last packet whatever the relay sends. With only 1
    spare kept, the pool must never land a real link on an unreliable spare and make things worse than without it -
    it does not need to be caught and switched off (POOL OFF) if it never gets the chance to go wrong."""
    w, ps, ws = _pool_room(new, 150.0, 4, out_only=True)
    w0, ps0, ws0 = _pool_room(False, 150.0, 4, out_only=True)
    got = [x for x in ws if x is not None]
    got0 = [x for x in ws0 if x is not None]
    never = sum(1 for x in ws if x is None)
    never0 = sum(1 for x in ws0 if x is None)
    ok = all(p.joins[0][2] for p in ps) and never <= never0 and (not got or not got0 or max(got) <= max(got0) + 1)
    off = w.lines("POOL OFF")
    return ok, _waits_txt(ws) + (f"; pool switched itself off after {off[0].split('s ')[0].strip()}s" if off else
                                  "; pool never needed to switch itself off" if new else "")


def sc_pool_open_host_untouched(new):
    """Control: a host with a forward or a public IP never gets the pool (no extra keepalives, no pool steers)."""
    w = World(True, pool=1 if new else 0)
    ps = [Player(w, ANDROID, "ANDROID-DOR", "106", 62000), Player(w, "192.0.2.97", "COOLDOR", "106", 20000),
          Player(w, THIRD, "StandaloneF0X", "106", 30000)]
    ps[0].join(30.0); ps[1].join(31.0); ps[2].join(60.0)
    for k in range(3):
        for i, p in enumerate(ps):
            p.game_link(200.0 + 120.0 * k + 0.2 * i, 280.0 + 120.0 * k, 40000 + 100 * i + k)
    w.sim.run_until(BASE + 620, w.mod)
    ws = [x for p in ps for x in p.waits()]
    pool_lines = w.lines("POOL", "pool:")
    ok = all(p.joins[0][2] and p.joins[0][1] == 1 for p in ps) and all(x is not None and x <= 2 for x in ws) and not pool_lines
    return ok, _waits_txt(ws) + f"; host judged '{w.mod.hold_state(HOST)}'; {len(pool_lines)} pool line(s)"


LINEBACKER_IP = "198.19.103.66"


def sc_revenge_room_link_rescue(new):
    """2026-11-15 04:59:52 - Cinema revenge swap, COOLDOR host (no forward): the room links come back on holes the host has
    not used for over a minute. LINEBACKER's hole had a rescue steer pending, the no-reply move waited 60 s for its ping
    (blocked: the link had already sent into that hole) and the game gave up on the room after ~58 s (booted to lobby)."""
    w = World(True, poison=True, out_only=True, pool=1)
    w.mod.RESCUE_NOWAIT = new
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, LINEBACKER_IP, "LINEBACKER", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    SIM.at(BASE + 150.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "open"}))   # as judged at 04:57:44
    w.host_load(196.0, 199.5)
    for i, p in enumerate(ps):
        p.pause(198.0)                               # room links quiet during the match (idle=16s at 04:58:00)
        p.game_link(200.0 + 0.2 * i, 330.0, 41000 + 100 * i)
    w.host_load(330.5, 338.0)                        # revenge swap load (host silent 7.7 s at 04:59:41)
    room = {}
    def note():
        for p in ps:
            room[p.user] = p.port
    SIM.at(BASE + 190.0, note)
    gave_up = {}
    def resume(p):
        port = room[p.user]
        start = SIM.t
        def loop():
            if SIM.t - start >= 58.0:                # the game's room timeout (LINEBACKER: 04:59:52 -> 05:00:50)
                if w.last_reply.get(port, -1e9) < start:
                    gave_up[p.user] = True
                    p._ctrl("reset_joiner")
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        p.reconnects.append((SIM.t, port))
        loop()
    for i, p in enumerate(ps):
        SIM.at(BASE + 339.0 + 0.3 * i, resume, p)
    w.sim.run_until(BASE + 420.0, w.mod)
    room_waits = {}
    for p in ps:
        t, port = p.reconnects[-1]
        ts = [x for x in w.reply_times.get(port, ()) if x >= t]
        room_waits[p.user] = ts[0] - t if ts else None
    ok = all(p.joins[0][2] for p in ps) and not gave_up and all(v is not None and v <= 20 for v in room_waits.values())
    txt = ", ".join(f"{u} {'never' if v is None else f'{v:.0f}s'}" for u, v in room_waits.items())
    return ok, f"room links back after revenge: {txt}" + (f"; booted to lobby: {', '.join(gave_up)}" if gave_up else "")


def sc_back_to_lobby_new_port(new):
    """2026-11-15 05:20:07 and 04:23:30 - back to the lobby: each joiner opens a new link, sends ~9 s, pauses ~7.7 s while
    loading, then carries on. LINEBACKER (PCSX2 Sockets over a VPN) carried on from a NEW port; the relay put it on his old
    room-link hole instead of the paused link's, the host saw another address and his game left the room 5 s later."""
    w = World(True)
    w.mod.REBIND_FOLLOW = new
    LB = LINEBACKER_IP
    who = [(ANDROID, "ANDROID-DOR", 62000), (DORIAN, "DORIAN_D", 20000), (LB, "LINEBACKER", 30000)]
    for i, (ip, user, port) in enumerate(who):
        w.ctrl(10.0 + 20 * i, op="map", joiner=ip, host=HOST, room="100", user=user)
        w.send(port, 10.2 + 20 * i, 110.8, ip=ip)               # room links, dropped as the lobby loads
        w.send(port + 100, 100.0 + 0.03 * i, 109.0, ip=ip)        # the back-to-lobby link: ~45 packets
    w.host_load(102.0, 110.0)
    w.send(62100, 116.7, 160.0, ip=ANDROID)                       # the other two carry on from the same socket
    w.send(20100, 116.7, 160.0, ip=DORIAN)
    w.send(30200, 117.0, 160.0, ip=LB)                            # LINEBACKER: same game socket, new port
    holes = {}
    SIM.at(BASE + 112.0, lambda: holes.update(paused=w.mod.RELAY_HOSTS[HOST]["src"].get((LB, 30100))))
    w.sim.run_until(BASE + 165.0, w.mod)
    src = w.mod.RELAY_HOSTS[HOST]["src"]
    came = src.get((LB, 30200))
    others = all(w.last_reply.get(p, -1e9) > BASE + 150 for p in (62100, 20100))
    ok = came is not None and came == holes.get("paused") and others and w.last_reply.get(30200, -1e9) > BASE + 150
    return ok, (f"LINEBACKER back from a new port on hole {came}, the paused link's hole {holes.get('paused')}; others answered"
                if ok else f"LINEBACKER back from a new port on hole {came}, but his paused link was on {holes.get('paused')} "
                           f"(the host sees another address)")


def sc_paused_link_keeps_hole(new):
    """2026-11-15 05:38:21 - revenge swap, COOLDOR host: LINEBACKER's room link got no reply and was moved onto the hole of
    DORIAN_D's room link, 7 s into DORIAN_D's load pause; DORIAN_D came back on a hole the host had not opened and left."""
    w = World(True)
    w.mod.OWNER_KEEP = new
    LB = LINEBACKER_IP
    w.ctrl(10.0, op="map", joiner=DORIAN, host=HOST, room="100", user="DORIAN_D")
    w.send(20000, 10.2, 126.0, ip=DORIAN)                 # DORIAN_D's room link...
    w.send(20000, 134.0, 160.0, ip=DORIAN)                # ...paused 8 s while the round loads
    w.ctrl(30.0, op="map", joiner=LB, host=HOST, room="100", user="LINEBACKER")
    w.send(30000, 30.2, 60.0, ip=LB)                      # LINEBACKER's room link, quiet during the match...
    w.send(30000, 107.0, 160.0, ip=LB)                    # ...back at the revenge swap, on a hole that has died
    w.ctrl(50.0, op="map", joiner=ANDROID, host=HOST, room="100", user="ANDROID-DOR")
    w.send(62000, 50.2, 160.0, ip=ANDROID)                # the host keeps talking to ANDROID-DOR
    holes = {}
    def kill():
        holes["d"] = w.mod.RELAY_HOSTS[HOST]["src"].get((DORIAN, 20000))
        holes["l"] = w.mod.RELAY_HOSTS[HOST]["src"].get((LB, 30000))
        w.block_in.add(holes["l"])                        # LINEBACKER's hole is gone on the host's router
    SIM.at(BASE + 100.0, kill)
    w.host_load(100.0, 106.0)
    w.sim.run_until(BASE + 165.0, w.mod)
    src = w.mod.RELAY_HOSTS[HOST]["src"]
    d_now, l_now = src.get((DORIAN, 20000)), src.get((LB, 30000))
    l_ok = w.last_reply.get(30000, -1e9) > BASE + 150
    ok = d_now == holes.get("d") and l_now not in (holes.get("l"), holes.get("d")) and l_ok
    return ok, (f"DORIAN_D kept hole {d_now}; LINEBACKER moved {holes.get('l')} -> {l_now}, answered" if ok else
                f"DORIAN_D hole {holes.get('d')} -> {d_now}; LINEBACKER {holes.get('l')} -> {l_now} "
                f"({'answered' if l_ok else 'not answered'})")


def sc_strict_host_not_open_after_load(new):
    """2026-11-15 04:57:44 and 05:15:51 - COOLDOR (no forward, UPnP off) judged 'open' at the instant its match-start
    load ended: the new game links went into never-pinged holes as the host began loading, and after the load the host's
    game sent to every player first, which opens those holes from its side. It is strict."""
    w = World(True, poison=True)
    w.mod.HOLD_RESUME_IGNORE = new
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000)]
    ps[0].join(30.0); ps[1].join(70.0)
    w.host_load(201.0, 208.0)                                 # the match-start load begins 1 s after the new links
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.05 * i, 260.0, 41000 + 100 * i)
    def host_first():                                         # after its load the host's game sends to every peer first
        hv = w.mod.RELAY_HOSTS[HOST]
        for e, hole in list(hv["src"].items()):
            if e[1] in (41000, 41100):
                w._host_reply(hole)
    SIM.at(BASE + 208.0, host_first)
    w.sim.run_until(BASE + 215.0, w.mod)
    state = w.mod.hold_state(HOST)
    ok = state != "open"
    return ok, f"host judged '{state}' after the match-start load" + ("" if ok else " (it is strict: holding switched off)")


def sc_open_verdict_downgrade(new):
    """2026-11-15 06:00-06:20 - COOLDOR (no forward, UPnP off) answered a never-pinged hole at once and was judged open, so
    nothing was held. At the Mount Hospital revenge swap both room links came back after a 100 s match into dead holes, one
    a second before the host's ping there was due (blocking it); DORIAN_D and ANDROID-DOR were dropped."""
    w = World(True, poison=True, pool=1)                 # as shipped
    w.mod.HOLD_DOWNGRADE = new
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000)]
    ps[0].join(30.0); ps[1].join(70.0)
    SIM.at(BASE + 120.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "open"}))   # the 0.1 s answer at 06:00:02
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)                                   # room links quiet for the match
    w.host_load(196.0, 199.5)
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 400.0, 41000 + 100 * i)
    w.host_load(400.5, 408.0)                            # revenge swap load
    waits = {}
    def resume(p):
        port, t0 = room[p.user], SIM.t
        def loop():
            if SIM.t - t0 >= 60.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()
        def check():
            ts = [x for x in w.reply_times.get(port, ()) if x >= t0]
            waits[p.user] = ts[0] - t0 if ts else None
        SIM.at(t0 + 59.0, check)
    for i, p in enumerate(ps):
        SIM.at(BASE + 399.0 + 0.3 * i, resume, p)
    w.sim.run_until(BASE + 470.0, w.mod)
    state = w.mod.hold_state(HOST)
    ok = all(v is not None and v <= 30 for v in waits.values()) and len(waits) == 2
    txt = ", ".join(f"{u} {'never' if v is None else f'{v:.0f}s'}" for u, v in waits.items())
    return ok, f"room links back after the revenge swap: {txt}; host judged '{state}'"


def sc_strict_used_hole_dies_early(new):
    """2026-11-15 06:40:07 - Factory revenge swap, COOLDOR (no forward, judged strict): ANDROID-DOR's room link came back
    on its own hole after 83 s of host silence. The relay trusted a used hole for 170 s, but this router drops one after
    ~45-60 s; the link's packets then blocked the host's ping there, no reply for 64 s, spectator."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)   # relay keepalives do not refresh this router
    w.mod.HOLD_COLD_STRICT_S = 60.0 if new else 0.0
    w.mod.WARM_AGE_S = 200.0        # in the real session the warm rotation (one steer per 25 s, pool steers in between)
                                    # did not reach that hole in time; a very long warm age stands in for that here
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    SIM.at(BASE + 120.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "strict"}))
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 285.0, 41000 + 100 * i)
    w.host_load(285.5, 293.0)
    waits = {}
    def resume(p):
        port, t0 = room[p.user], SIM.t
        def loop():
            if SIM.t - t0 >= 70.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()
        def check():
            ts = [x for x in w.reply_times.get(port, ()) if x >= t0]
            waits[p.user] = ts[0] - t0 if ts else None
        SIM.at(t0 + 69.0, check)
    for i, p in enumerate(ps):
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p)
    w.sim.run_until(BASE + 360.0, w.mod)
    ok = len(waits) == 3 and all(v is not None and v <= 12 for v in waits.values())
    txt = ", ".join(f"{u} {'never' if v is None else f'{v:.0f}s'}" for u, v in waits.items())
    return ok, f"room links back after the swap (used holes die after 50 s): {txt}"


def sc_rescue_to_fresh_pinged_hole(new):
    """2026-11-15 06:40:20 - Factory revenge swap, the rescue path with the cold-age fix OFF: ANDROID-DOR's room link is
    sent into its dead hole, a rescue steer there is blocked by its own packets, every proven hole has a live link. In the
    real session a freshly pinged pool spare (45010) was free and the no-reply move would not take it (proven holes only):
    35 s. Here no such spare exists at that moment, so the move has nothing to take either way; this is a regression check
    (no worse than before) - the real fix for the case is HOLD_COLD_STRICT_S (sc_strict_used_hole_dies_early)."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)
    w.mod.HOLD_COLD_STRICT_S = 0.0            # the link IS sent into the dead hole (the cold-age fix off): test the rescue
    w.mod.WARM_AGE_S = 200.0
    w.mod.RESCUE_FRESH = new
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    SIM.at(BASE + 120.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "strict"}))
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 340.0, 41000 + 100 * i)   # the game links keep going through the swap, as
                                                                 # at 06:40:21: every proven hole has a live link
    w.host_load(285.5, 293.0)
    waits = {}
    def resume(p):
        port, t0 = room[p.user], SIM.t
        def loop():
            if SIM.t - t0 >= 70.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()
        def check():
            ts = [x for x in w.reply_times.get(port, ()) if x >= t0]
            waits[p.user] = ts[0] - t0 if ts else None
        SIM.at(t0 + 69.0, check)
    for i, p in enumerate(ps):
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p)
    w.sim.run_until(BASE + 360.0, w.mod)
    ok = len(waits) == 3 and all(v is not None for v in waits.values())
    txt = ", ".join(f"{u} {'never' if v is None else f'{v:.0f}s'}" for u, v in waits.items())
    return ok, f"room links back after the swap, cold-age fix off (rescue path only): {txt}"


SCENARIOS = [sc_leave_freeze, sc_leave_then_rejoin, sc_inbound_block, sc_outbound_block, sc_failed_steer_rejoin,
             sc_no_false_move, sc_join_split_steer, sc_remapped_srp_third_joiner,
             sc_fast_third_joiner, sc_reuse_other_live_hole,
             sc_strict_nat_second_joiner, sc_strict_nat_leave_rejoin_stress, sc_open_host_not_held,
             sc_strict_held_link_follows_ping, sc_strict_map_load_reconnects,
             sc_open_host_slow_reconnect, sc_public_ip_host_open,
             sc_strict_pool_map_change_4p, sc_strict_pool_long_matches, sc_strict_pool_router_ignores_keepalives,
             sc_pool_open_host_untouched, sc_revenge_room_link_rescue, sc_back_to_lobby_new_port, sc_paused_link_keeps_hole,
             sc_strict_host_not_open_after_load, sc_open_verdict_downgrade,
             sc_strict_used_hole_dies_early, sc_rescue_to_fresh_pinged_hole]


def sc_age_log(new):
    """AGE log (log only), on the Factory reproduction: each room link back after the swap writes one AGE line with the
    host's silence on its hole, the cold verdict, the steps taken and the wait for the host's first packet. old = AGE off.
    That no relay decision changes is checked by running the whole suite with SCCT_RELAY_AGE_LOG=0 and 1."""
    saved = os.environ.get("SCCT_RELAY_AGE_LOG")
    os.environ["SCCT_RELAY_AGE_LOG"] = "1" if new else "0"
    try:
        sc_strict_used_hole_dies_early(True)
    finally:
        if saved is None:
            os.environ.pop("SCCT_RELAY_AGE_LOG", None)
        else:
            os.environ["SCCT_RELAY_AGE_LOG"] = saved
    age = [m for t, m in SIM.logs if m.startswith("AGE ")]
    if not new:
        return not age, f"AGE off: {len(age)} AGE lines"
    room = [m for m in age if "kind=resume" in m and "quiet=8" in m]
    android = [m for m in room if "ANDROID-DOR" in m]
    ok = (len(room) == 3 and len(android) == 1 and "host_data=88s" in android[0] and "cold=yes" in android[0]
          and "ready spare" in android[0] and "outcome=answered wait=0.0s" in android[0])
    return ok, (f"{len(age)} AGE lines, {len(room)} for the room links back after the swap; ANDROID-DOR: "
                + (android[0].split("kind=", 1)[1] if android else "missing"))


SCENARIOS.append(sc_age_log)


def sc_untested_host_noreply_verdict(new):
    """2026-11-15 16:02-16:14 - COOLDOR (no forward, UPnP off) stayed untested for 12 minutes: the first test was set aside
    (the host's first burst after a load). At the 16:08 swap DORIAN_D's room link went straight into a dead hole and got no
    reply for 27 s; at the next swap (16:14) a room link still waited 29 s, because holds and ready spares only start once
    the host is judged strict. Here: room links back at two swaps on a router that drops used holes after 50 s."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)
    w.mod.HOLD_NOREPLY_STRICT = new
    w.mod.WARM_AGE_S = 200.0          # as in sc_strict_used_hole_dies_early: the warm rotation does not reach the holes
    w.mod.HOLD_DETECT_LATE_S = 1e9    # no other test comes: in the session the match-start links landed on holes the host
                                      # had just pinged (16:02:45, 16:08:19), so the late test had nothing to judge
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    # the host's first test was set aside: untested when the matches start
    SIM.at(BASE + 150.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "unknown"}))
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)
    waits = {1: {}, 2: {}}
    state = {}

    def resume(p, swap):
        port, t0 = room[p.user], SIM.t

        def loop():
            if SIM.t - t0 >= 70.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()

        def check():
            ts = [x for x in w.reply_times.get(port, ()) if x >= t0]
            waits[swap][p.user] = ts[0] - t0 if ts else None
        SIM.at(t0 + 69.0, check)
    # match 1, swap at 284 (room links back 1 s before the host's load), match 2, swap at 469
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 285.0, 41000 + 100 * i)
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p, 1)
    w.host_load(285.5, 293.0)
    SIM.at(BASE + 400.0, lambda: state.update(after_swap1=w.mod.hold_state(HOST)))
    for i, p in enumerate(ps):
        p.game_link(356.0 + 0.2 * i, 470.0, 42000 + 100 * i)
        SIM.at(BASE + 469.0 + 0.3 * i, resume, p, 2)
    w.host_load(470.5, 478.0)
    w.sim.run_until(BASE + 545.0, w.mod)
    w2 = waits[2]
    ok = (state.get("after_swap1") == "strict" and len(w2) == 3
          and all(v is not None and v <= 12 for v in w2.values()))
    fmt = lambda d: ", ".join(f"{u} {'never' if v is None else f'{v:.0f}s'}" for u, v in d.items())
    return ok, (f"host after swap 1: '{state.get('after_swap1')}'; swap 1: {fmt(waits[1])}; swap 2: {fmt(w2)}")


SCENARIOS.append(sc_untested_host_noreply_verdict)


def sc_own_socket_keeps_hole(new):
    """2026-11-15 17:25:49 - Krauser map start, COOLDOR host (strict): DORIAN_D's PS2 rotates three fixed sockets. Socket
    10073 came back on a dead hole and got the "ready spare" 45009, the hole socket 10071 had carried all match and had left
    20 s before (10071 had come back after a long silence twice already). At the revenge swap 10071 returned, found 45009
    taken, got another hole; the host stopped sending to 45009 and DORIAN_D's PS2 left the room. Here: the same rotation.
    The simulated host answers any address, so the check is the hand-off itself: one hole, one socket."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)
    w.mod.OWN_SOCKET_KEEP = new
    w.mod.WARM_AGE_S = 200.0          # as in the session: no ping reached 10073's hole for 295 s before it came back
    dor = Player(w, DORIAN, "DORIAN_D", "100", 20000)
    andr = Player(w, ANDROID, "ANDROID-DOR", "100", 62000)
    dor.join(30.0); andr.join(70.0)
    SIM.at(BASE + 120.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "strict"}))
    s1, s2 = 20001, 20002                       # DORIAN_D's other two fixed sockets (10071 and 10073 in the session)
    dor.game_link(125.0, 135.0, s1)             # s1 used once, then back after 65 s: a socket that returns
    dor.game_link(140.0, 150.0, s2)             # s2 used earlier: it has a hole of its own (dead by the time it returns)
    dor.pause(198.0); andr.pause(198.0)
    dor.game_link(200.0, 285.0, s1)             # match 4 on s1
    andr.game_link(200.2, 285.0, 41000)
    w.host_load(285.5, 293.0)                   # map change
    dor.game_link(305.0, 400.0, s2)             # 20 s after s1 left: s2 is back on its dead hole (match 5)
    andr.game_link(304.0, 400.0, 41100)       # just before: takes the freshly pinged spare, as other links did at 17:25:05-28
    seen = {}

    def snap(tag):
        src = w.mod.RELAY_HOSTS[HOST].get("src", {})
        seen[tag] = (src.get((DORIAN, s1)), src.get((DORIAN, s2)))
    SIM.at(BASE + 290.0, snap, "match4")
    SIM.at(BASE + 330.0, snap, "match5")
    w.host_load(400.5, 408.0)                   # revenge swap
    dor.game_link(402.0, 470.0, s1)             # s1 comes back
    SIM.at(BASE + 440.0, snap, "revenge")
    w.sim.run_until(BASE + 475.0, w.mod)
    waits = dor.waits()
    h1 = seen.get("match4", (None, None))[0]    # s1's hole in match 4
    h2 = seen.get("match5", (None, None))[1]    # s2's hole in match 5
    shared = h1 is not None and h1 == h2
    last = waits[-1] if waits else None
    ok = (not shared and h2 is not None and last is not None and last <= 25
          and all(v is not None for v in waits[-2:]))
    return ok, (f"match 5: s1 hole {h1}, s2 hole {h2}" + (" (s2 took s1's hole)" if shared else " (own holes)")
                + f"; s2 answered after {waits[-2]:.0f}s" if len(waits) >= 2 and waits[-2] is not None else "")  \
        + (f"; s1 back at the swap answered after {last:.0f}s" if last is not None else "; s1 back at the swap never answered")


SCENARIOS.append(sc_own_socket_keeps_hole)


def sc_own_socket_long_halves(new):
    """Real match length: two 10-minute halves, revenge swap, return to the lobby, three players back together each time.
    DORIAN_D's PS2 rotates three fixed sockets (each has come back once before); a socket that sits out a half loses its
    idle hole to another link meanwhile. The first own-socket version only protected against a socket that still had a
    hole, so the socket back at the map start was given the hole another of his sockets had carried all half."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)
    w.mod.OWN_SOCKET_KEEP = new
    s1, s2, s3 = 10071, 10072, 10073
    dor = Player(w, DORIAN, "DORIAN_D", "100", 20000)
    andr = Player(w, ANDROID, "ANDROID-DOR", "100", 62000)
    thr = Player(w, THIRD, "Wickedness", "100", 30000)
    dor.join(30.0); andr.join(50.0); thr.join(70.0)

    def link(ip, port, t0, t1):
        def loop():
            if SIM.t >= BASE + t1:
                return
            w._joiner_pkt(port, ip)
            SIM.at(SIM.t + 0.2, loop)
        SIM.at(BASE + t0, loop)
    for i, s in enumerate((s1, s2, s3)):            # each socket used, then back after 60 s+ once
        link(DORIAN, s, 100.0 + 5 * i, 104.0 + 5 * i)
        link(DORIAN, s, 180.0 + 5 * i, 184.0 + 5 * i)
    for p in (dor, andr, thr):
        p.pause(195.0)
    link(DORIAN, s1, 200.0, 800.0); link(ANDROID, 41000, 200.2, 800.0); link(THIRD, 42000, 200.4, 800.0)   # half 1
    w.host_load(800.5, 808.0)                                                                             # revenge swap
    link(DORIAN, s3, 799.0, 839.0); link(ANDROID, 41050, 799.3, 839.0); link(THIRD, 30000, 799.6, 839.0)
    link(DORIAN, s2, 820.0, 1420.0); link(ANDROID, 41100, 820.2, 1420.0); link(THIRD, 42100, 820.4, 1420.0)  # half 2
    w.host_load(1420.5, 1428.0)                                                                           # back to lobby
    link(DORIAN, s1, 1419.0, 1459.0); link(ANDROID, 41150, 1419.3, 1459.0); link(THIRD, 30000, 1419.6, 1459.0)
    handoffs, prev = [], {}

    def tick():
        hv = w.mod.RELAY_HOSTS.get(HOST)
        if hv:
            cur = {s: hv.get("src", {}).get((DORIAN, s)) for s in (s1, s2, s3)}
            for s, h in cur.items():
                if h is None or prev.get(s) == h:
                    continue
                for y, hy in prev.items():
                    if y != s and hy == h and (DORIAN, y) in hv.get("returned", set()):
                        handoffs.append(f"{SIM.t - BASE:.0f}s {s} took {y}'s hole {h}")
            prev.clear(); prev.update(cur)
        SIM.at(SIM.t + 1.0, tick)
    SIM.at(BASE + 1.0, tick)
    w.sim.run_until(BASE + 1500.0, w.mod)
    waits, never = [], 0
    for t, m in SIM.logs:
        if m.startswith("AGE ") and (BASE + 798 <= t <= BASE + 880 or BASE + 1418 <= t <= BASE + 1500):
            if "outcome=never" in m:
                never += 1
            else:
                waits.append(float(m.split("wait=")[1].split("s")[0]))
    ok = not handoffs and never == 0 and waits and max(waits) <= 25
    return ok, (f"sibling hand-offs of a returning socket: {handoffs or 'none'}; at the two swaps {len(waits)} answered, "
                f"worst {max(waits) if waits else 0:.0f}s, never {never}")


SCENARIOS.append(sc_own_socket_long_halves)


def sc_pool_stepdown_fox_miss(new):
    """2026-11-15 23:59:29 - FOX (no forward). A kept spare was silent 34 s, just under the 35 s trust, and did not
    answer JMIC. One miss switched the pool off; Cinema returns then waited 59-66 s. Step-down keeps the pool and
    trusts spares for one ping cycle (20 s). Ported from Grok's draft; the second probe is changed: a spare silent 14 s
    after the step is a hole the host pinged moments ago, so it is not put on trial and cannot switch the pool off.

    The spare is planted. pick_hole_port prefers the hole the host pinged a few seconds ago, so a joiner sent at
    "ping + 32 s" never lands on the spare that has actually been silent that long (the first cut of this scenario
    did that, and neither side ever called pool_miss)."""
    w = World(True, poison=True, pool=1, out_only=True)
    w.mod.POOL_STEPDOWN = bool(new)
    p = Player(w, DORIAN, "DORIAN_D", "100", 20000)
    p.join(10.0)
    SIM.at(BASE + 40.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "strict", "probe": {}}))
    result = {}
    JMIC = "198.19.253.212"

    def pulse(hole, until):
        # pool_tick only counts a miss if the host is answering some other hole right now
        if hole is None or SIM.t > until:
            return
        w.mod.HOST_DATA.setdefault(HOST, {})[hole] = SIM.t
        SIM.at(SIM.t + 1.0, pulse, hole, until)

    def plant(ip, port, age):
        """One kept spare, silent `age` seconds, NAT already dead. Every other free hole is too old to be chosen."""
        mod = w.mod
        now = SIM.t
        hv = mod.RELAY_HOSTS.get(HOST)
        if hv is None:
            hv = {"joiner": (DORIAN, 20000), "sock": w.jsock, "rx": 0, "tx": 0, "last": now,
                  "first": now, "eps": {}, "dump_rx": 0, "dump_tx": 0}
            mod.RELAY_HOSTS[HOST] = hv
        src = hv.setdefault("src", {})
        eps = hv.setdefault("eps", {})
        for ep in list(src):
            if ep[0] in (JMIC, THIRD):
                src.pop(ep, None)
                eps.pop(ep, None)
                hv.get("conn", {}).pop(ep, None)
                hv.get("probe", set()).discard(ep)
        live_ep = (DORIAN, p.port or 20000)
        live = src.get(live_ep) or mod.RELAY_HOLES[0]
        src[live_ep] = live
        eps[live_ep] = (w.jsock, now)
        spare = next(pt for pt in mod.RELAY_HOLES if pt != live and not any(
            pp == pt and mod.holds_hole(hv, e, (ip, port), now) for e, pp in src.items()))
        w.clt.hole_target = live          # the ping target is taken, so it is not a "ready" hole
        w.clt.hole_last = now - 1.0
        traffic = mod.HOST_TRAFFIC.setdefault(HOST, {})
        hdata = mod.HOST_DATA.setdefault(HOST, {})
        rtraffic = mod.RELAY_TRAFFIC.setdefault(HOST, {})
        ext = mod.HOST_EXT.setdefault(HOST, {})
        st = mod.pool_st(HOST)
        st["trial"].clear()
        for pt in mod.RELAY_HOLES:
            if pt == live:
                continue
            st["life"].pop(pt, None)
            traffic[pt] = now - 400.0
        traffic[live] = max(traffic.get(live, 0.0), now - 1.0)
        hdata[live] = now
        ext.setdefault(live, 3658)
        # 29 s at the full 35 s ceiling: still inside trust after the 5 s trial (logged silence ~34 s, the FOX miss).
        # 14 s after the step: above the shortened trial gate (12 s) and still inside 20 s after the 5 s trial.
        traffic[spare] = now - age
        hdata[spare] = now - age
        rtraffic[spare] = now - 1.0
        ext[spare] = 3658
        st["life"][spare] = [now - age, now - age]
        w.open_until[spare] = now - 1.0
        w.assured.discard(spare)
        w.opened.discard(spare)
        w.poisoned.pop(spare, None)
        return hv, spare, live

    def burst(ip, port, seconds=7.5):
        t0 = SIM.t

        def loop():
            if SIM.t - t0 >= seconds:
                return
            w._joiner_pkt(port, ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()

    def probe():
        w.mod.relay_control(json.dumps({
            "op": "map", "joiner": JMIC, "host": HOST, "room": "100", "user": "JMIC"}).encode(), ("127.0.0.1", 1))
        hv, spare, live = plant(JMIC, 41000, 29.0)
        result["spare1"] = spare
        burst(JMIC, 41000)
        result["landed1"] = hv["src"].get((JMIC, 41000))
        result["trial1"] = spare in w.mod.pool_st(HOST).get("trial", {})
        result["alive1"] = w.mod.pool_alive(HOST, spare, SIM.t)
        pulse(live, BASE + 130.0)

        def after_first():
            st = w.mod.POOL.get(HOST, {})
            result["off1"] = bool(st.get("off"))
            result["trust1"] = w.mod.pool_trust(HOST)
            result["miss1"] = st.get("misses")
            result["step"] = bool(w.lines("POOL step down"))
            result["off_line"] = bool(w.lines("POOL OFF"))
            if not st.get("off"):
                SIM.at(SIM.t + 2.0, probe2)
        SIM.at(SIM.t + 8.0, after_first)

    def probe2():
        w.mod.relay_control(json.dumps({
            "op": "map", "joiner": THIRD, "host": HOST, "room": "100", "user": "StandaloneF0X"}).encode(),
            ("127.0.0.1", 1))
        hv, spare, _live = plant(THIRD, 41001, 14.0)
        result["spare2"] = spare
        burst(THIRD, 41001)
        result["landed2"] = hv["src"].get((THIRD, 41001))
        result["trial2"] = spare in w.mod.pool_st(HOST).get("trial", {})
        result["alive2"] = w.mod.pool_alive(HOST, spare, SIM.t)

        def after_second():
            st = w.mod.POOL.get(HOST, {})
            result["off2"] = bool(st.get("off"))
            result["trust2"] = w.mod.pool_trust(HOST)
            result["miss2"] = st.get("misses")
        SIM.at(SIM.t + 8.0, after_second)

    SIM.at(BASE + 80.0, probe)
    w.sim.run_until(BASE + 130.0, w.mod)
    if new:
        ok = (result.get("step") and not result.get("off1") and not result.get("off_line")
              and result.get("trust1") == w.mod.POOL_TRUST_STEP_S and result.get("trial1")
              and result.get("landed1") == result.get("spare1")
              and not result.get("off2") and not result.get("trial2") and result.get("trust2") == w.mod.POOL_TRUST_STEP_S
              and result.get("landed2") == result.get("spare2"))
        txt = ("first miss (spare silent ~34 s) stepped trust down to 20 s, pool stayed on; a later link on a 14 s spare "
               "was not trialed and the pool stayed on" if ok else f"result={result}")
    else:
        ok = (result.get("off1") and result.get("off_line") and not result.get("step")
              and result.get("trial1") and result.get("landed1") == result.get("spare1"))
        txt = ("first miss switched the pool off (no step-down)" if ok else f"result={result}")
    return ok, txt


SCENARIOS.append(sc_pool_stepdown_fox_miss)



def sc_shadow_upnp_router(new):
    """2026-11-16 - COOLDOR with UPnP on: fresh holes reached his PC, a recently used hole (45001) did not, and the relay's
    one open/strict verdict could not say so. Stage 1 shadow lines (SCCT_RELAY_SHADOW, log only): three joins and two swaps
    on a router whose forward lets fresh holes in and drops holes used in the last minutes. Every SHADOW test verdict is
    checked against what the router did with those packets; routing is the same with the lines on or off (cmp check)."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True, upnp=120.0)
    w.mod.SHADOW = new
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)

    def resume(p):
        port, t0 = room[p.user], SIM.t

        def loop():
            if SIM.t - t0 >= 60.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 285.0, 41000 + 100 * i)
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p)
    w.host_load(285.5, 293.0)
    for i, p in enumerate(ps):
        p.game_link(356.0 + 0.2 * i, 470.0, 42000 + 100 * i)
        SIM.at(BASE + 469.0 + 0.3 * i, resume, p)
    w.host_load(470.5, 478.0)
    w.sim.run_until(BASE + 545.0, w.mod)
    tests = [(t, m) for t, m in SIM.logs if m.startswith("SHADOW test")]
    links = [m for t, m in SIM.logs if m.startswith("SHADOW link")]
    if not new:
        return (not tests and not links), f"shadow off: {len(tests)} test lines, {len(links)} link lines"
    import re as _re
    wrong, kinds = [], {"OK": 0, "NO REPLY": 0, "ambiguous": 0, "no evidence": 0}
    for t, m in tests:
        hole = int(_re.search(r"hole=(\d+)", m).group(1))
        t0 = t - float((_re.search(r"(?:answered|NO REPLY for) ([\d.]+)s", m) or [0, "0"])[1]) if "s after" in m or "NO REPLY" in m else t
        got = [d for (tt, h, d) in w.deliveries if h == hole and t0 - 0.1 <= tt <= t]   # log times are rounded to 0.1 s
        kind = "OK" if ": OK" in m else "NO REPLY" if "NO REPLY" in m else "ambiguous" if "ambiguous" in m else "no evidence"
        kinds[kind] += 1
        if kind == "OK" and not any(got):
            wrong.append(f"OK but nothing was delivered: {m}")
        if kind == "NO REPLY" and any(got):
            wrong.append(f"NO REPLY but {sum(got)} packets were delivered: {m}")
    ev = w.mod.SHADOW_EV.get(HOST, {})
    ok = not wrong and kinds["OK"] >= 1 and kinds["NO REPLY"] >= 1 and len(links) >= 3
    msg = (f"tests: {kinds['OK']} OK, {kinds['NO REPLY']} no-reply, {kinds['ambiguous']} ambiguous, {kinds['no evidence']} "
           f"no evidence; wrong verdicts {len(wrong)}; fresh {ev.get('fresh')}, stale {ev.get('stale')}; link lines "
           f"{len(links)}, would-moves {ev.get('would', 0)}" + (f"; FIRST WRONG: {wrong[0][:160]}" if wrong else ""))
    return ok, msg


SCENARIOS.append(sc_shadow_upnp_router)



def sc_upnp_hint_not_relayed(new):
    """2026-11-16 - scopsDNS 2.1 sends {"op":"upnp_open"} to 45000 from the host's PC, i.e. from the host's public IP. The
    relay took any non-ping datagram from a relayed host's IP as game data and sent it to the joiner, so the hint went into
    the joiner's game every 20 minutes. Now it is logged and dropped (SCCT_RELAY_UPNP_HINT)."""
    w = World(True)
    w.mod.UPNP_HINT = new
    p = Player(w, ANDROID, "ANDROID-DOR", "100", 62000)
    p.join(30.0)
    w.sim.run_until(BASE + 40.0, w.mod)
    before = sum(w.got.values())
    w.mod.handle_packet(b'{"op":"upnp_open"}', (HOST, 51234), SIM.t, 45000)
    after = sum(w.got.values())
    logged = any(m.startswith("SHADOW hint") for _, m in SIM.logs)
    relayed = after - before
    ok = (relayed == 0 and logged) if new else True
    return ok, f"hint datagrams relayed to the joiner: {relayed}; logged: {'yes' if logged else 'no'}"


SCENARIOS.append(sc_upnp_hint_not_relayed)



def sc_shadow_probe_proof(new):
    """2026-11-16 17:53 - COOLDOR with UPnP: every fresh-hole test fell in a map-load burst (ambiguous), so the shadow lines
    could never call fresh holes proven. The SCOPS window's router probe (UDP 3659 test port, SCCT_RELAY_UPNP_PROBE) is
    evidence that does not depend on the game. Here passive proof is made impossible; with a good probe result logged before
    the second swap (new) the held returns get 'WOULD ... (router probe ...)' lines, without it (old) none."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True, upnp=120.0)
    w.mod.SHADOW = True
    w.mod.SHADOW_PROVEN = 99          # passive tests alone can never prove fresh holes (COOLDOR: all ambiguous)
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)

    def resume(p):
        port, t0 = room[p.user], SIM.t

        def loop():
            if SIM.t - t0 >= 60.0:
                return
            w._joiner_pkt(port, p.ip)
            SIM.at(SIM.t + 0.2, loop)
        loop()
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 285.0, 41000 + 100 * i)
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p)
    w.host_load(285.5, 293.0)
    if new:
        SIM.at(BASE + 400.0, lambda: w.mod._shadow_ev(HOST).update(probe={
            "fresh_ok": 2, "fresh_miss": 0, "stale_ok": 0, "stale_miss": 1, "t": SIM.t,
            "last_fresh_ok": 2, "last_fresh_miss": 0}))
    for i, p in enumerate(ps):
        p.game_link(356.0 + 0.2 * i, 470.0, 42000 + 100 * i)
        SIM.at(BASE + 469.0 + 0.3 * i, resume, p)
    w.host_load(470.5, 478.0)
    w.sim.run_until(BASE + 545.0, w.mod)
    would = [m for t, m in SIM.logs if m.startswith("SHADOW link") and "WOULD" in m]
    by_probe = [m for m in would if "router probe" in m]
    early = [m for t, m in SIM.logs if m.startswith("SHADOW link") and "WOULD" in m and t < BASE + 400.0]
    ok = (len(by_probe) >= 1 and not early) if new else not would
    return ok, (f"WOULD lines {len(would)} (by router probe {len(by_probe)}, before the probe {len(early)})")


SCENARIOS.append(sc_shadow_probe_proof)


def _fail_world(fail):
    """The sc_untested_host_noreply_verdict session (no-reply move, strict verdict, bad hole) with FAIL_LOG on or off."""
    w = World(True, poison=True, pool=1, used_life=50.0, out_only=True)
    w.mod.FAIL_LOG = fail
    w.mod.HOLD_NOREPLY_STRICT = True
    w.mod.WARM_AGE_S = 200.0
    w.mod.HOLD_DETECT_LATE_S = 1e9
    ps = [Player(w, ANDROID, "ANDROID-DOR", "100", 62000), Player(w, DORIAN, "DORIAN_D", "100", 20000),
          Player(w, THIRD, "Wickedness", "100", 30000)]
    ps[0].join(30.0); ps[1].join(70.0); ps[2].join(110.0)
    SIM.at(BASE + 150.0, lambda: w.mod.HOST_HOLD.__setitem__(HOST, {"state": "unknown"}))
    room = {}
    SIM.at(BASE + 190.0, lambda: room.update({p.user: p.port for p in ps}))
    for p in ps:
        p.pause(198.0)
    w.host_load(196.0, 199.5)

    def resume(p):
        port, t0 = room[p.user], SIM.t

        def loop():
            if SIM.t - t0 < 70.0:
                w._joiner_pkt(port, p.ip)
                SIM.at(SIM.t + 0.2, loop)
        loop()
    for i, p in enumerate(ps):
        p.game_link(200.0 + 0.2 * i, 285.0, 41000 + 100 * i)
        SIM.at(BASE + 284.0 + 0.3 * i, resume, p)
    w.host_load(285.5, 293.0)
    for i, p in enumerate(ps):
        p.game_link(356.0 + 0.2 * i, 470.0, 42000 + 100 * i)
        SIM.at(BASE + 469.0 + 0.3 * i, resume, p)
    w.host_load(470.5, 478.0)
    w.sim.run_until(BASE + 545.0, w.mod)
    return w, list(SIM.logs)


def _routing_lines(logs):
    out = []
    for t, m in logs:
        if m.startswith(("FAIL why", "NAT step", "FAIL log error")):
            continue
        m = re.sub(r" avoided=\S+", "", m)
        m = m.split(" | evidence: ")[0]
        out.append((round(t, 3), m))
    return out


def sc_fail_steps_log_only(new):
    """2026-11-16 (Grok's ask after COOLDOR's UPnP evening) - say where a link failed: FAIL why on a no-reply move (when the
    host last used the dead hole, what it was answering, the router probe), the avoided holes on AGE lines, and NAT step
    lines for a console socket stuck at PORT_ID. Log only: the same session with SCCT_RELAY_FAIL_LOG on and off must make
    exactly the same routing decisions and deliveries."""
    w_on, logs_on = _fail_world(True)
    w_off, logs_off = _fail_world(False)
    same = (_routing_lines(logs_on) == _routing_lines(logs_off) and w_on.deliveries == w_off.deliveries)
    why = [m for _, m in logs_on if m.startswith("FAIL why")]
    avoided = [m for _, m in logs_on if m.startswith("AGE ") and " avoided=" in m]
    extra_off = [m for _, m in logs_off if m.startswith(("FAIL why", "NAT step")) or " avoided=" in m]
    # NAT step: a socket that got its PORT_ID and never pinged, then the session closes
    mod = w_on.mod
    c = Clt()
    c.addr, c.port = "203.0.113.9", 10070
    t = SIM.t
    c.reqs = {1: {"sock": "1", "pending": False, "t": t - 20}, 2: {"sock": "3", "pending": True, "t": t - 11}}
    mod.CLIENTS.append(c)
    n0 = len(SIM.logs)
    mod.nat_step_tick(t)
    mod.nat_step_tick(t + 1)                    # once only
    mod.nat_step_waiting(c, t + 2, "closed its NAT session (FIN)")
    nat = [m for _, m in SIM.logs[n0:] if m.startswith("NAT step")]
    mod.CLIENTS.remove(c)
    nat_ok = len(nat) == 2 and "socket 3 stopped at 'PORT_ID sent'" in nat[0] and "sockets done: 1" in nat[0] \
        and "socket(s) 3" in nat[1]
    ok = same and bool(why) and not extra_off and nat_ok if new else True
    return ok, (f"routing identical on/off: {'yes' if same else 'NO'}; FAIL why lines {len(why)}, AGE lines with avoided "
                f"holes {len(avoided)}, NAT step lines {len(nat)}; extra lines with it off {len(extra_off)}"
                + (f"\n      e.g. {why[0][:260]}" if why else ""))


SCENARIOS.append(sc_fail_steps_log_only)


def _guard_world(on, pps=300):
    """The FAIL-steps session (three joins, two map loads with bursts, a no-reply move) with the relay guard on or off."""
    orig = World.__init__
    w_holder = {}

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.mod.GUARD_LOG = on
        self.mod.GUARD_PPS = pps
        self.mod.GUARD_LINKS.clear()
        self.mod.GUARD_UNMAPPED.clear()
        w_holder["w"] = self
    World.__init__ = init
    try:
        w, logs = _fail_world(True)
    finally:
        World.__init__ = orig
    return w, logs


def sc_guard_log_only(new):
    """2026-11-18 - relay guard (idea from tubisoft's 187 relay: per-player packet-rate and packet-size caps). Log only: with
    SCCT_RELAY_GUARD_LOG on, even with an absurdly low limit (3 pkt/s), routing and deliveries must be exactly the same as
    with it off; the would-limit and peak lines must appear; unmapped senders are counted and still dropped."""
    w_off, logs_off = _guard_world(False)
    w_on, logs_on = _guard_world(True)
    w_low, logs_low = _guard_world(True, pps=3)
    strip = lambda L: [(t, m) for t, m in _routing_lines(L) if not m.startswith("GUARD")]
    same = (strip(logs_on) == strip(logs_off) and strip(logs_low) == strip(logs_off)
            and w_on.deliveries == w_off.deliveries and w_low.deliveries == w_off.deliveries)
    off_lines = [m for _, m in logs_off if m.startswith("GUARD")]
    would = [m for _, m in logs_low if m.startswith("GUARD would-limit")]
    would_normal = [m for _, m in logs_on if m.startswith("GUARD would-limit")]
    mod = w_low.mod
    peaks = max((g["pk_n"] for g in mod.GUARD_LINKS.values()), default=0)
    n0 = len(SIM.logs)
    import types
    fake = types.SimpleNamespace(getsockname=lambda: ("0.0.0.0", 45003))
    mod.relay_from_joiner(fake, b"x" * 40, ("198.18.250.7", 5555), SIM.t)    # never mapped by the lobby
    mod.relay_from_joiner(fake, b"x" * 40, ("198.18.250.7", 5555), SIM.t)
    mod._GUARD_LAST[0] = SIM.t - mod.GUARD_SUMMARY_S - 1
    mod.guard_tick(SIM.t)
    summ = [m for _, m in SIM.logs[n0:] if m.startswith("GUARD ")]
    unm = [m for m in summ if m.startswith("GUARD unmapped") and "198.18.250.7 2 pkt" in m]
    pk = [m for m in summ if m.startswith("GUARD peaks")]
    ok = same and not off_lines and bool(would) and not would_normal and bool(unm) and bool(pk) if new else True
    return ok, (f"routing and deliveries identical off / on / on with 3 pkt/s limit: {'yes' if same else 'NO'}; "
                f"would-limit lines at 3 pkt/s {len(would)}, at 300 pkt/s {len(would_normal)}; busiest link {peaks} pkt/s; "
                f"guard lines with it off {len(off_lines)}; unmapped sender counted: {'yes' if unm else 'NO'}"
                + (f"\n      e.g. {would[0][:200]}" if would else "") + (f"\n      e.g. {pk[0][:240]}" if pk else ""))


SCENARIOS.append(sc_guard_log_only)


def _fair_state(on):
    """Tonight's 00:36:02 moment: the host pings 45001, which the relay kept warm for DORIAN_D's idle lobby socket; SNAKE's
    lobby socket arrives 0.1 s before DORIAN_D's."""
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    mod.FAIR_SHADOW = on
    mod.FAIR_WARM_FOR.clear(); mod.FAIR_TAKEN.clear()
    mod.HOLD_MODE = "off"
    mod.RELAY_HOLES = list(range(45001, 45013))
    t = 5000.0
    c = Clt(); c.hole_target, c.hole_last, c.last_seen = 45001, t - 13, t
    mod.CLIENTS.append(c)
    mod.HOST_TRAFFIC[HOST] = {45001: t - 13, 45003: t - 0.5, 45006: t - 0.3}
    mod.HOST_DATA[HOST] = {45001: t - 820, 45003: t - 6, 45006: t - 6}
    owner, taker = (DORIAN, 10072), (THIRD, 10072)
    hv = {"joiner": (THIRD, 10071), "sock": None, "rx": 0, "tx": 0, "last": t, "first": t - 3000,
          "eps": {owner: (None, t - 821), taker: (None, t - 837), (THIRD, 10071): (None, t - 0.2), (DORIAN, 10071): (None, t - 0.3)},
          "src": {owner: 45001, (THIRD, 10071): 45003, (DORIAN, 10071): 45006}}
    mod.RELAY_HOSTS[HOST] = hv
    mod.RELAY_MAP[DORIAN] = {"host": HOST, "user": "DORIAN_D"}
    mod.RELAY_MAP[THIRD] = {"host": HOST, "user": "SNAKE"}
    mod.fair_warm_steer(HOST, 45001, owner, t - 20)
    a = mod.pick_hole_port(hv, taker, t, None, HOST)
    hv["eps"][taker] = (None, t)                        # SNAKE's link is now live on 45001
    b = mod.pick_hole_port(hv, owner, t + 0.1, None, HOST)
    mod._FAIR_LAST[0] = t - mod.FAIR_SUMMARY_S - 1
    mod.fair_tick(t + 1)
    return (a, b), [m for _, m in SIM.logs]


def sc_fair_shadow_log_only(new):
    """2026-11-19 - fair-handout shadow (log only). Replays STRIDERSTROKE's 00:36:02 return: SNAKE's lobby link takes the
    hole kept warm for DORIAN_D. With SCCT_RELAY_FAIR_SHADOW on, the same holes must be chosen as with it off, and the
    FAIR lines must name the taken hole, the owner's return and the summary; a full session must route identically."""
    (on_ports, on_logs), (off_ports, off_logs) = _fair_state(True), _fair_state(False)
    strip = lambda L: [m for m in L if not m.startswith("FAIR")]
    same_pick = on_ports == off_ports and strip(on_logs) == strip(off_logs)
    took = [m for m in on_logs if m.startswith("FAIR shadow: hole 45001 kept warm for DORIAN_D")]
    back = [m for m in on_logs if m.startswith("FAIR shadow: DORIAN_D") and "back 0.1s" in m]
    summ = [m for m in on_logs if m.startswith("FAIR summary") and "taken by another player 1" in m]
    off_lines = [m for m in off_logs if m.startswith("FAIR")]
    # a whole session (three joins, two map loads, a no-reply move) routes the same with it on and off
    orig = World.__init__
    def mk(flag):
        def init(self, *a, **k):
            orig(self, *a, **k)
            self.mod.FAIR_SHADOW = flag
            self.mod.FAIR_WARM_FOR.clear(); self.mod.FAIR_TAKEN.clear()
        return init
    World.__init__ = mk(True)
    try:
        w1, l1 = _fail_world(True)
    finally:
        World.__init__ = orig
    World.__init__ = mk(False)
    try:
        w0, l0 = _fail_world(True)
    finally:
        World.__init__ = orig
    st = lambda L: [(t, m) for t, m in _routing_lines(L) if not m.startswith("FAIR")]
    same_run = st(l1) == st(l0) and w1.deliveries == w0.deliveries
    ok = same_pick and same_run and bool(took) and bool(back) and bool(summ) and not off_lines if new else True
    return ok, (f"same holes on/off: {'yes' if same_pick else 'NO'} ({on_ports}); whole session identical: "
                f"{'yes' if same_run else 'NO'}; taken line {len(took)}, owner-back line {len(back)}, summary {len(summ)}, "
                f"lines with it off {len(off_lines)}" + (f"\n      e.g. {took[0][:230]}" if took else "")
                + (f"\n      e.g. {back[0][:230]}" if back else ""))


SCENARIOS.append(sc_fair_shadow_log_only)


def _fast_mod(on):
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    mod.FAST_RESUME_LOG = on
    mod.FAST_WATCH.clear()
    for k in mod.FAST_STATS:
        mod.FAST_STATS[k] = 0
    mod.RELAY_MAP[DORIAN] = {"host": HOST, "user": "IY0"}
    mod.RELAY_MAP[THIRD] = {"host": HOST, "user": "DORIAN_D"}
    mod.RELAY_HOSTS.clear()          # reset_joiner then only does the leave check
    return mod


def _fast_rec(mod, ip, port, t0, quiet):
    return {"t0": t0, "host": HOST, "ep": (ip, port), "kind": "resume", "quiet": quiet, "state": "strict",
            "ctx": "in-game", "hole": 45009, "cur": 45009, "data": t0 - 555, "any": t0 - 9, "cold": False, "events": [], "bad": ""}


def sc_fast_resume_log_only(new):
    """2026-11-20 - fast resume flag (log only). Replays 2026-11-19 23:46:20 (SNAKE strict host): IY0's link quiet 689 s is
    answered in 0.0 s at a round end and IY0's console leaves the room 8.8 s later. Also a normal return (answered after
    10 s: no flag), a fast answer the player stays through (follow-up 'stayed'), and a whole session that must route the
    same with the flag on and off."""
    mod = _fast_mod(True)
    t = mod.time.monotonic() - 8.8     # reset_joiner reads the wall clock: the leave comes 8.8 s after the answer
    mod.age_emit(_fast_rec(mod, DORIAN, 10071, t, 689), t + 0.0, "answered")       # IY0, the 23:46:20 case
    mod.age_emit(_fast_rec(mod, THIRD, 10071, t + 0.2, 683), t + 15.6, "answered")  # DORIAN_D, normal 15.4 s wait
    mod.reset_joiner(DORIAN, "GROUP_LEAVE room 107 (IY0)")                          # wall clock: within the watch window
    t2 = t + 600
    mod.age_emit(_fast_rec(mod, THIRD, 10073, t2, 900), t2 + 0.1, "answered")      # fast answer, player stays
    mod.fast_resume_tick(t2 + 0.1 + mod.FAST_WATCH_S + 1)
    L = [m for _, m in SIM.logs]
    flags = [m for m in L if m.startswith("FAST RESUME: ")]
    left = [m for m in L if m.startswith("FAST RESUME follow-up") and "LEFT the room" in m and "IY0" in m]
    stayed = [m for m in L if m.startswith("FAST RESUME follow-up") and "stayed in the room" in m]
    normal_flag = [m for m in flags if "683s" in m]
    off = _fast_mod(False)
    t = off.time.monotonic() - 8.8
    off.age_emit(_fast_rec(off, DORIAN, 10071, t, 689), t, "answered")
    off.reset_joiner(DORIAN, "GROUP_LEAVE room 107 (IY0)")
    off_lines = [m for _, m in SIM.logs if m.startswith("FAST")]
    orig = World.__init__
    def mk(flag):
        def init(self, *a, **k):
            orig(self, *a, **k)
            self.mod.FAST_RESUME_LOG = flag
            self.mod.FAST_WATCH.clear()
        return init
    runs = []
    for flag in (True, False):
        World.__init__ = mk(flag)
        try:
            runs.append(_fail_world(True))
        finally:
            World.__init__ = orig
    st = lambda L: [(t_, m) for t_, m in _routing_lines(L) if not m.startswith("FAST")]
    same_run = st(runs[0][1]) == st(runs[1][1]) and runs[0][0].deliveries == runs[1][0].deliveries
    ok = (len(flags) == 2 and bool(left) and bool(stayed) and not normal_flag and not off_lines and same_run) if new else True
    return ok, (f"fast flags {len(flags)} (want 2), left line {len(left)}, stayed line {len(stayed)}, normal return flagged "
                f"{len(normal_flag)}, lines with it off {len(off_lines)}, whole session identical: {'yes' if same_run else 'NO'}"
                + (f"\n      e.g. {flags[0][:220]}" if flags else "") + (f"\n      e.g. {left[0][:220]}" if left else ""))


SCENARIOS.append(sc_fast_resume_log_only)


def _upnp_open_mod(on):
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    mod.UPNP_OPEN_SHADOW = on
    for k in mod.UPNP_OPEN_STATS:
        mod.UPNP_OPEN_STATS[k] = 0
    mod.HOLD_MODE = "auto"
    mod.HOLD_NOREPLY_STRICT = True
    mod.NAT_NAMES[HOST] = "COOLDOR"
    mod.HOST_HOLD.clear()
    return mod


def sc_upnp_open_shadow_log_only(new):
    """2026-11-21 - UPnP-open shadow (log only). Replays 01:19:16: COOLDOR (PCSX2 Sockets, UPnP forward confirmed, router
    probe fresh 4/4) is judged strict when ANDROID-DOR gets no reply for 18 s on hole 45002 (host silent there 144 s,
    last ping 46 s before). With the flag on: one 'WOULD keep it open' line; a host with no UPnP report gets 'strict
    stands'; with the flag off no lines; the verdict itself is the same either way; a whole session routes the same."""
    out = {}
    for on in (True, False):
        mod = _upnp_open_mod(on)
        now = mod.time.monotonic()
        mod.HOST_DATA[HOST] = {45002: now - 18 - 144, 45004: now}
        mod.HOST_TRAFFIC[HOST] = {45002: now - 18 - 46, 45004: now}
        mod.SHADOW_HINT[HOST] = now - 774
        mod.SHADOW_EV[HOST] = {"fresh": [0, 0, 2], "stale": [0, 1, 5], "would": 6, "links": 6,
                               "probe": {"fresh_ok": 4, "fresh_miss": 0, "stale_ok": 1, "stale_miss": 1}}
        mod.hold_noreply_verdict(HOST, 45002, now - 18, now, "ANDROID-DOR 159.26.103.66:10071")
        state = mod.hold_state(HOST)
        other = "203.0.113.9"
        mod.HOST_HOLD[other] = {"state": "unknown"}
        mod.HOST_DATA[other] = {45002: now - 300, 45004: now}
        mod.HOST_TRAFFIC[other] = {45002: now - 100, 45004: now}
        mod.hold_noreply_verdict(other, 45002, now - 18, now, "X 1.2.3.4:10071")
        out[on] = (state, mod.hold_state(other), [m for _, m in SIM.logs if m.startswith("UPNP_OPEN")])
    would = [m for m in out[True][2] if "WOULD keep it open" in m and "COOLDOR" in m and "4/4" in m]
    stands = [m for m in out[True][2] if "strict stands" in m and "203.0.113.9" in m]
    orig = World.__init__
    def mk(flag):
        def init(self, *a, **k):
            orig(self, *a, **k)
            self.mod.UPNP_OPEN_SHADOW = flag
        return init
    runs = []
    for flag in (True, False):
        World.__init__ = mk(flag)
        try:
            runs.append(_fail_world(True))
        finally:
            World.__init__ = orig
    st = lambda L: [(t_, m) for t_, m in _routing_lines(L) if not m.startswith("UPNP_OPEN")]
    same_run = st(runs[0][1]) == st(runs[1][1]) and runs[0][0].deliveries == runs[1][0].deliveries
    same_verdict = out[True][0] == out[False][0] == "strict" and out[True][1] == out[False][1] == "strict"
    ok = (len(would) == 1 and len(stands) == 1 and not out[False][2] and same_verdict and same_run) if new else True
    return ok, (f"would-open line {len(would)} (want 1), strict-stands line {len(stands)} (want 1), lines with it off "
                f"{len(out[False][2])}, verdicts unchanged: {'yes' if same_verdict else 'NO'}, whole session identical: "
                f"{'yes' if same_run else 'NO'}" + (f"\n      e.g. {would[0][:230]}" if would else ""))


SCENARIOS.append(sc_upnp_open_shadow_log_only)


def sc_pause_shadow_log_only(new):
    """2026-11-21 - pause shadow (log only). Replays 03:20:29: COOLDOR judged strict when ANDROID-DOR's match-start link got
    nothing back on cold hole 45004 for 4 s; the host's last data on its other holes (45001-45003) was 2 s before the
    verdict and it then sent nothing anywhere for 7 s (match about to start). With the flag on: one 'WOULD cancel' line
    for that case, one 'verdict stands' line for a host that kept sending; with it off no lines; the verdict itself is
    unchanged; a whole session routes the same."""
    out = {}
    for on in (True, False):
        global SIM
        SIM = Sim()
        mod = load_relay()
        mod.log = lambda m: SIM.logs.append((SIM.t, m))
        mod.PAUSE_SHADOW = on
        mod.PAUSE_WATCH.clear()
        for k in mod.PAUSE_STATS:
            mod.PAUSE_STATS[k] = 0
        mod.NAT_NAMES[HOST] = "COOLDOR"
        T = 1000.0
        # case 1: the 03:20:29 pause - last data elsewhere 2 s before, nothing again until T+5
        mod.HOST_DATA[HOST] = {45001: T - 2.0, 45002: T - 2.0, 45003: T - 2.1}
        mod.pause_shadow_note(HOST, 45004, T, "nothing back on cold hole 45004")
        for i in range(1, 10):
            if i >= 5:
                for p_ in (45001, 45002, 45003):
                    mod.HOST_DATA[HOST][p_] = T + i
            mod.pause_shadow_tick(T + i)
        # case 2: a host that keeps sending game data every 0.2 s on another hole
        other = "203.0.113.9"
        mod.HOST_DATA[other] = {45001: T + 20 - 0.2}
        mod.pause_shadow_note(other, 45004, T + 20, "nothing back on cold hole 45004")
        for i in range(1, 10):
            mod.HOST_DATA[other][45001] = T + 20 + i - 0.2
            mod.pause_shadow_tick(T + 20 + i)
        out[on] = [m for _, m in SIM.logs if m.startswith("PAUSE")]
    cancel = [m for m in out[True] if "WOULD cancel" in m and "COOLDOR" in m]
    stands = [m for m in out[True] if "verdict stands" in m and "203.0.113.9" in m]
    errs = [m for m in out[True] if "error" in m]
    orig = World.__init__
    def mk(flag):
        def init(self, *a, **k):
            orig(self, *a, **k)
            self.mod.PAUSE_SHADOW = flag
        return init
    runs = []
    for flag in (True, False):
        World.__init__ = mk(flag)
        try:
            runs.append(_fail_world(True))
        finally:
            World.__init__ = orig
    st = lambda L: [(t_, m) for t_, m in _routing_lines(L) if not m.startswith("PAUSE")]
    same_run = st(runs[0][1]) == st(runs[1][1]) and runs[0][0].deliveries == runs[1][0].deliveries
    pause_in_run = [m for _, m in runs[0][1] if m.startswith("PAUSE")] if isinstance(runs[0][1][0], tuple) else []
    ok = (len(cancel) == 1 and len(stands) == 1 and not errs and not out[False] and same_run) if new else True
    return ok, (f"would-cancel line {len(cancel)} (want 1), verdict-stands line {len(stands)} (want 1), errors {len(errs)}, "
                f"lines with it off {len(out[False])}, whole session identical: {'yes' if same_run else 'NO'} "
                f"(PAUSE lines in that session: {len(pause_in_run)})" + (f"\n      e.g. {cancel[0][:240]}" if cancel else ""))


SCENARIOS.append(sc_pause_shadow_log_only)


def sc_pause_aware_verdict(new):
    """2026-11-21 - pause-aware strict verdict (SCCT_RELAY_PAUSE_AWARE). Drives the real cold-hole detector (hold_detect):
    (A) COOLDOR 04:16:27 - a join link 4.1 s unanswered on cold hole 45009, the host's last data elsewhere 1.8 s before,
        then silent everywhere: with the switch on the verdict is cancelled and the host stays untested; off: strict.
    (B) a real strict host that keeps sending to its other players every 0.2 s: strict with the switch on too, under 1 s
        later than with it off.
    (C) the cap: after 2 cancels the 3rd paused test gives strict at once.
    (D) whole-session replays (no-reply/strict world) with the switch on and off: same verdicts and same deliveries."""
    def mk(on):
        global SIM
        SIM = Sim()
        mod = load_relay()
        mod.log = lambda m: SIM.logs.append((SIM.t, m))
        mod.HOLD_MODE = "auto"
        mod.PAUSE_AWARE = on
        mod.HOST_HOLD.clear()
        mod.HOST_DATA.clear()
        mod.HOST_TRAFFIC.clear()
        mod.RELAY_MAP.clear()
        return mod
    ep = ("203.0.113.50", 10072)
    def paused_case(mod, host, T):
        st = mod.HOST_HOLD.setdefault(host, {"state": "unknown"})
        st.setdefault("probe", {})[45009] = [T - 4.1, 11, ep]
        mod.HOST_DATA[host] = {45001: T - 1.8, 45002: T - 1.8, 45003: T - 1.9}
        out = []
        for dt in (0.0, 1.0, 2.0, 3.2, 4.0):
            mod.hold_detect(T + dt)
            out.append(mod.hold_state(host))
        return out
    res = {}
    for on in (True, False):
        mod = mk(on)
        a = paused_case(mod, HOST, 1000.0)
        # B: real strict host
        host_b = "198.51.100.7"
        st = mod.HOST_HOLD.setdefault(host_b, {"state": "unknown"})
        T = 2000.0
        st["probe"] = {45009: [T - 4.1, 11, ep]}
        mod.HOST_DATA[host_b] = {45001: T - 0.2}
        t_strict = None
        for k in range(0, 40):
            t = T + k * 0.1
            mod.HOST_DATA[host_b][45001] = t - 0.05
            mod.hold_detect(t)
            if mod.hold_state(host_b) == "strict":
                t_strict = t - T
                break
        # C: cap
        host_c = "198.51.100.8"
        caps = []
        for i in range(3):
            caps.append(paused_case(mod, host_c, 3000.0 + 100 * i)[-1])
        res[on] = (a, t_strict, caps, [m for _, m in SIM.logs if m.startswith("HOLD") or m.startswith("PAUSE")])
    a_on, a_off = res[True][0], res[False][0]
    okA = a_on[-1] == "unknown" and a_off[0] == "strict"
    okB = res[True][1] is not None and res[False][1] is not None and res[True][1] - res[False][1] < 1.0
    okC = res[True][2] == ["unknown", "unknown", "strict"]
    errs = [m for m in res[True][3] if "error" in m]
    cancel = [m for m in res[True][3] if "verdict cancelled" in m]
    orig = World.__init__
    def mkw(flag):
        def init(self, *a, **k):
            orig(self, *a, **k)
            self.mod.PAUSE_AWARE = flag
        return init
    runs = []
    for flag in (True, False):
        World.__init__ = mkw(flag)
        try:
            runs.append(_fail_world(True))
        finally:
            World.__init__ = orig
    verd = lambda L: [m.split(":")[0] + (" strict" if "is strict" in m else "") for _, m in L if "is strict" in m]
    lines = lambda L: [m for _, m in L if " is strict" in m or " is open" in m]
    same = runs[0][0].deliveries == runs[1][0].deliveries and len(lines(runs[0][1])) == len(lines(runs[1][1]))
    ok = (okA and okB and okC and same and not errs) if new else True
    return ok, (f"A paused: on {a_on} / off first={a_off[0]} ({'ok' if okA else 'BAD'}); B real strict: verdict after "
                f"{res[True][1]}s on vs {res[False][1]}s off ({'ok' if okB else 'BAD'}); C cap: {res[True][2]} "
                f"({'ok' if okC else 'BAD'}); D whole session same deliveries/verdicts: {'yes' if same else 'NO'}; errors {len(errs)}"
                + (f"\n      e.g. {cancel[0][:230]}" if cancel else ""))


SCENARIOS.append(sc_pause_aware_verdict)


def sc_pause_group_count(new):
    """2026-11-21 05:21 - DORIAN_D and ANDROID-DOR joined COOLDOR's room 0.02 s apart; their second links (45009, 45010)
    opened 0.1 s apart and both ran into the same ~5 s host pause, so both strict verdicts were cancelled and the
    per-host limit of 2 was used up by one pause. Now: (1) that replay counts 1 cancel, not 2; (2) a later, separate
    pause is cancel 2; (3) a third separate pause gives strict at once (cap still works); (4) a real strict host that
    keeps sending to others is strict even while a pause-cancel window from another link is fresh? - it is: the
    window only lets the CHECK start, the check itself still sees the host sending and confirms strict."""
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    mod.HOLD_MODE = "auto"
    mod.PAUSE_AWARE = True
    for d in (mod.HOST_HOLD, mod.HOST_DATA, mod.HOST_TRAFFIC, mod.RELAY_MAP):
        d.clear()
    h = HOST
    def pause(T, ports):
        st = mod.HOST_HOLD.setdefault(h, {"state": "unknown"})
        pr = st.setdefault("probe", {})
        for k, port in enumerate(ports):
            pr[port] = [T - 4.1 + 0.1 * k, 11, ("203.0.113.%d" % (50 + k), 10072)]
        mod.HOST_DATA[h] = {45001: T - 1.5, 45002: T - 1.5}
        for dt in (0.0, 0.1, 1.0, 2.0, 3.1, 3.2, 4.0):
            mod.hold_detect(T + dt)
        return mod.hold_state(h), mod.HOST_HOLD[h].get("pause_cancels", 0)
    r1 = pause(1000.0, [45009, 45010])
    r2 = pause(1100.0, [45011])
    r3 = pause(1200.0, [45012])
    # (4) real strict host right after its own same-pause window: two links, host keeps sending
    h2 = "198.51.100.9"
    st = mod.HOST_HOLD.setdefault(h2, {"state": "unknown", "pause_cancels": 2, "pause_last_cancel": 2000.0})
    st["probe"] = {45009: [2001.0 - 4.1, 11, ("203.0.113.60", 10072)]}
    mod.HOST_DATA[h2] = {45001: 2001.0}
    t4 = None
    for k in range(30):
        t = 2001.0 + k * 0.1
        mod.HOST_DATA[h2][45001] = t - 0.05
        mod.hold_detect(t)
        if mod.hold_state(h2) == "strict":
            t4 = t - 2001.0
            break
    lines = [m for _, m in SIM.logs if "verdict cancelled" in m]
    same = [m for m in lines if "same pause" in m]
    ok1 = r1 == ("unknown", 1) and len(same) == 1
    ok2 = r2 == ("unknown", 2)
    ok3 = r3[0] == "strict"
    ok4 = t4 is not None and t4 < 1.0
    ok = (ok1 and ok2 and ok3 and ok4) if new else True
    return ok, (f"05:21 replay (2 links, one pause): state/cancels {r1} want ('unknown', 1) {'ok' if ok1 else 'BAD'}; "
                f"2nd pause {r2} {'ok' if ok2 else 'BAD'}; 3rd pause -> {r3[0]} {'ok' if ok3 else 'BAD'}; real strict host "
                f"strict after {t4}s {'ok' if ok4 else 'BAD'}" + (f"\n      e.g. {same[0][:230]}" if same else ""))


SCENARIOS.append(sc_pause_group_count)


def _verdict_mod(strict):
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    mod.HOLD_MODE = "auto"
    mod.HOLD_RESUME_IGNORE = True
    mod.OPEN_STRICT = strict
    mod.OPEN_PROOF = 2
    mod.HOST_HOLD[HOST] = {"state": "unknown"}
    mod.RELAY_HOSTS[HOST] = {"host_active_since": 0.0}
    mod.HOST_DATA[HOST] = {45001: 0.0}
    return mod


def _probe(mod, hole, t0, answer, joiner=DORIAN, burst_start=None):
    """A player's first packet into cold hole `hole` at t0; the host answers there at `answer` (None: never). The host keeps
    sending game data on 45001; burst_start: the host was silent (map load) and its first burst began then."""
    mod.HOST_HOLD[HOST].setdefault("probe", {})[hole] = [t0, mod.HOLD_DETECT_PKTS + 5, (joiner, 10071)]
    mod.RELAY_MAP[joiner] = {"host": HOST, "t": t0}
    if burst_start is not None:
        mod.RELAY_HOSTS[HOST]["host_active_since"] = burst_start
        mod.HOST_DATA[HOST][45001] = burst_start
    if answer is not None:
        mod.HOST_DATA[HOST][hole] = answer
        mod.hold_detect(answer + 0.05)
    else:
        for k in range(1, 12):
            t = t0 + 0.5 * k
            mod.HOST_DATA[HOST][45001] = t
            mod.hold_detect(t)
    return mod.HOST_HOLD[HOST].get("state")


def _open_clean_through_send(strict, pause=False):
    """02:37:40 through relay_from_joiner. The probe timer is dropped and started again on a used hole, so the answer
    looks 1.2 s late; the player has been sending since 1000.0 (16.2 s)."""
    logs = []
    mod = load_relay()
    mod.log = lambda m: logs.append(m)
    mod.OPEN_STRICT = strict
    mod.OPEN_PROOF = 2
    mod.HOLD_MODE = "auto"
    mod.AGE_LOG = False
    mod.SHADOW = False
    mod.NAT_MAIN_SOCK = FakeSock(45000, lambda *a: None)
    for p in mod.RELAY_HOLES:
        mod.HOLE_SOCKS[p] = FakeSock(p, lambda *a: None)
    ep = (DORIAN, 10071)
    mod.RELAY_MAP[ep[0]] = {"host": HOST, "t": 0.0}
    mod.RELAY_HOSTS[HOST] = {
        "joiner": ep, "sock": mod.NAT_MAIN_SOCK, "rx": 0, "tx": 0, "last": 1000.0, "first": 1000.0,
        "eps": {}, "dump_rx": 0, "dump_tx": 0, "src": {ep: 45008},
    }
    mod.HOST_DATA[HOST] = {45008: 700.0, 45001: 1000.0}
    mod.HOST_TRAFFIC[HOST] = {45008: 700.0, 45001: 1000.0}
    mod.HOST_HOLD[HOST] = {"state": "unknown"}
    for i in range(81):
        t = 1000.0 + i * 0.2
        if pause and 1006.0 < t < 1014.5:
            continue           # the player goes quiet for 8.5 s mid-wait (map load), then sends again
        mod.HOST_DATA[HOST][45001] = t
        mod.HOST_TRAFFIC[HOST][45001] = t
        mod.relay_from_joiner(mod.NAT_MAIN_SOCK, b"\x07\x00x", ep, t)
        if i % 5 == 4:
            mod.hold_detect(t)
    mod.HOST_DATA[HOST][45008] = 1016.2
    mod.hold_detect(1016.25)
    st = mod.HOST_HOLD[HOST]
    late = any("too late to prove open" in m and "16.2s" in m and ("probe timer 1.2s" in m or pause) for m in logs)
    started = (st.get("link0") or {}).get(45008, [None])[0]
    return st.get("state"), st.get("open_hits"), late, started


def sc_open_verdict_strict(new):
    """2026-11-16 23:17:04 and 2026-11-17 02:37:40 - a strict host was judged open. SCCT_RELAY_OPEN_STRICT=1: a map-load
    burst never counts, a clean answer must be within 2 s of the player's real first packet on that hole, and two clean
    answers are needed. Strict detection is unchanged. Switch off keeps today's behaviour, including both false opens."""
    # A: the 23:17 order - burst starts on 45001 at 100.000, the player's first packet into 45006 at 100.010, answer 100.050
    mod = _verdict_mod(new)
    a = _probe(mod, 45006, 100.010, 100.050, burst_start=100.000)
    # B: a genuinely open host - clean answers 0.1 s after the first packet, host sending steadily the whole time
    mod = _verdict_mod(new)
    mod.HOST_DATA[HOST][45001] = 49.9
    b1 = _probe(mod, 45002, 50.0, 50.1)
    mod.HOST_DATA[HOST][45001] = 79.9
    b2 = _probe(mod, 45003, 80.0, 80.1)
    # C: a strict host - no answer while the host sends on 45001
    mod = _verdict_mod(new)
    mod.HOST_DATA[HOST][45001] = 199.9
    c = _probe(mod, 45004, 200.0, None)
    # D: the old guard still works (burst began after the first packet)
    mod = _verdict_mod(new)
    d = _probe(mod, 45005, 300.0, 309.2, burst_start=309.0)
    # E: 2026-11-17 02:37:40. The probe timer was reset on a hole the host had used before, so the answer looked
    # 0.3 s / 3.9 s late. The player had already been sending for 16 s. With the switch on that is not a clean answer.
    mod = _verdict_mod(new)
    mod.HOST_HOLD[HOST]["link0"] = {45008: [100.0, 115.7, (DORIAN, 10071)], 45009: [100.0, 115.9, (DORIAN, 10071)]}
    e1 = _probe(mod, 45008, 115.7, 116.0)
    e2 = _probe(mod, 45009, 115.9, 116.2)
    e_logs = [m for _, m in SIM.logs]
    # F: an answer 0.4 s after the real first packet still counts (link0 agrees with the probe timer; 1 of 2, not open yet)
    mod = _verdict_mod(new)
    mod.HOST_DATA[HOST][45001] = 49.9
    mod.HOST_HOLD[HOST]["link0"] = {45002: [50.0, 50.0, (DORIAN, 10071)]}
    f = _probe(mod, 45002, 50.0, 50.4)
    f_logs = [m for _, m in SIM.logs]
    got = f"23:17 order -> {a}; open host after 1st clean answer -> {b1}, after 2nd -> {b2}; strict host -> {c}; " \
          f"burst after first packet -> {d}; 02:37 late pair -> {e1},{e2}; 0.4s answer -> {f}"
    send_state, send_hits, send_late, send_t0 = _open_clean_through_send(new)
    got += f"; through send -> {send_state} hits={send_hits} late={send_late} first={send_t0}"
    # G: the same, but the player pauses 8.5 s mid-wait: the first packet must still be 1000.0, not 1014.6
    p_state, p_hits, p_late, p_t0 = _open_clean_through_send(new, pause=True)
    got += f"; with an 8.5s pause -> {p_state} late={p_late} first={p_t0}"
    if new:
        e_late = sum("too late to prove open" in m for m in e_logs) == 2 and not any("clean answer" in m for m in e_logs)
        f_clean = any("clean answer 1 of 2" in m for m in f_logs) and not any("too late to prove open" in m for m in f_logs)
        ok = a == "unknown" and b1 == "unknown" and b2 == "open" and c == "strict" and d == "unknown" \
            and e1 == "unknown" and e2 == "unknown" and f == "unknown" and e_late and f_clean \
            and send_state == "unknown" and send_hits is None and send_late and send_t0 == 1000.0 \
            and p_state == "unknown" and p_late and p_t0 == 1000.0
        if not (e_late and f_clean):
            got += f"; late-log={e_late} fast-log={f_clean}"
    else:   # today's behaviour, kept with the switch off: the 23:17 false open, and the 02:37 one
        ok = a == "open" and b1 == "open" and c == "strict" and d == "unknown" and e1 == "open" and f == "open" \
            and not any("too late to prove open" in m for m in e_logs + f_logs) \
            and send_state == "open" and not send_late and send_t0 is None
    return ok, got


SCENARIOS.append(sc_open_verdict_strict)


def sc_host_nat_lost_log(new):
    """2026-11-17 16:15:05 - COOLDOR's console stopped acknowledging steers (router hardware acceleration off), then closed
    its NAT session (FIN) mid-room; its hole pings stopped and two returning players were held until the game dropped them.
    Log only: a HOST line when a hosting console's NAT session ends, one line when it then pings without a session, and
    nothing for a normal logout after the room closed. Routing is not touched."""
    logs = []
    mod = load_relay()
    mod.log = lambda m: logs.append(m)
    mod.FAIL_LOG = new
    mod.NAT_NAMES[HOST] = "COOLDOR"
    mod.NAT_NAMES[DORIAN] = "DORIAN_D"
    mod.RELAY_MAP[DORIAN] = {"host": HOST, "room": "100", "user": "DORIAN_D"}
    mod.HOST_DATA[HOST] = {45003: 1000.0}
    c = Clt()
    c.addr, c.port = HOST, 10070
    c.reqs = {}
    mod.nat_step_waiting(c, 1001.0, "closed its NAT session (FIN)")
    mod.nat_step_orphan_ping((HOST, 10071), 1010.0)
    mod.nat_step_orphan_ping((HOST, 10072), 1010.1)      # once only
    fin = [m for m in logs if m.startswith("NAT step: HOST") and "closed its NAT session" in m]
    orphan = [m for m in logs if "pings without a NAT session" in m]
    n0 = len(logs)
    mod.HOST_DATA[HOST] = {45003: 1000.0}
    mod.nat_step_waiting(c, 1200.0, "closed its NAT session (FIN)")   # room over: no game data for 200 s
    later = logs[n0:]
    if new:
        ok = len(fin) == 1 and "DORIAN_D" in fin[0] and len(orphan) == 1 and "closed it 9s ago" in orphan[0] and not later
    else:
        ok = not fin and not orphan and not later
    return ok, (f"HOST lines {len(fin)}, orphan-ping lines {len(orphan)}, lines for a logout after the room {len(later)}"
                + (f"\n      e.g. {fin[0][:220]}" if fin else ""))


SCENARIOS.append(sc_host_nat_lost_log)


def sc_loop_stats_log_only(new):
    """Loop timing (SCCT_RELAY_LOOP_STATS) is log only: off in code; when on it counts game packets into the right
    latency buckets, sums housekeeping and pauses (and which pauses had packets waiting), prints one LOOP STATS line per
    window with traffic, none for an idle window, and resets after each window."""
    global SIM
    SIM = Sim()
    mod = load_relay()
    mod.log = lambda m: SIM.logs.append((SIM.t, m))
    forced = os.environ.get("SCCT_RELAY_LOOP_STATS", "0").strip().lower() in ("1", "on", "true", "yes")
    off_in_code = (mod.LOOP_STATS is False) or forced   # forced on by the caller for a full run
    if not new:
        return off_in_code, f"switch off in code: {off_in_code}"
    mod.LOOP_STATS_S = 300.0
    st = mod.LoopStats(1000.0)
    for dt in [0.00005] * 90 + [0.0008] * 8 + [0.006, 0.030]:   # 100 game packets: 90 fast, 8 <1ms, one 6ms, one 30ms
        st.packet(True, dt)
    st.packet(False, 0.002)
    for dt in (0.0002, 0.0004, 0.0031):
        st.housekeeping(dt)
    st.pause(0.025, True, True)
    st.pause(0.026, False, False)
    st.tick(1100.0)                      # inside the window: no line yet
    early = len(SIM.logs)
    st.tick(1300.5)                      # window over: one line, then reset
    lines = [m for _, m in SIM.logs if m.startswith("LOOP STATS")]
    ln = lines[0] if lines else ""
    st.tick(1601.0)                      # idle window: no line
    lines2 = [m for _, m in SIM.logs if m.startswith("LOOP STATS")]
    want = ["game pkts 100", "p99 <10ms", "max 30.0ms", ">=5ms 2", "other pkts 1", "housekeeping 3 passes",
            "max 3.1ms", "pauses 2 total 51ms max 26ms", "with packets waiting 1 (game 1)"]
    missing = [w for w in want if w not in ln]
    p50 = st.pct(0.5)
    ok = off_in_code and early == 0 and len(lines) == 1 and not missing and len(lines2) == 1 and st.game_n == 0
    return ok, (f"off in code {off_in_code}; lines before window end {early}; lines {len(lines)} then after idle window "
                f"{len(lines2)}; missing fields {missing or 'none'}\n      {ln[:300]}")


SCENARIOS.append(sc_loop_stats_log_only)


def main():
    allok = True
    for sc in SCENARIOS:
        print(f"\n{sc.__name__}: {sc.__doc__.strip().splitlines()[0]}")
        for new in (False, True):
            ok, msg = sc(new)
            print(f"  {'new' if new else 'old'}: {'OK  ' if ok else 'FAIL'} {msg}")
            if new:
                allok &= ok
            if VERBOSE and new:
                for t, m in SIM.logs:
                    if any(k in m for k in ("RELAY host", "HOLE steer", "HOLE bad", "reset joiner", "no reply", "RELAY ctrl", "POOL", "HOLD")):
                        print(f"      {t - BASE:7.1f}s {m}")
    print("\nall new-behaviour scenarios pass" if allok else "\nSOME NEW-BEHAVIOUR SCENARIOS FAIL")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
