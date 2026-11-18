#!/usr/bin/env python3
"""End-to-end check of the SRP-by-session fix (SCCT_RELAY_SRP_BY_SESSION) with the relay's real SRP/NAT code: only the
sockets and the clock are replaced. A host logs in (SYN, four NAT requests, ping), three players join 30 s apart, the
host logs out. Run for a host whose router moved the SRP session to port 4465 (2026-11-13, Snake) and for one on 10070.

    python relay_e2e_srp.py
"""
import importlib.util, json, os, pathlib, struct, sys, itertools
BETA = str(pathlib.Path(__file__).resolve().parent.parent.parent)
sys.path.insert(0, BETA + "/pylib")
os.environ["SCCT_RELAY_HOLES"] = ",".join(str(p) for p in range(45001, 45013))
os.environ["SCCT_RELAY_WARM_HOLES"] = "1"
os.environ["SCCT_RELAY_SNAPSHOT"] = ""
_n = itertools.count()

def load():
    spec = importlib.util.spec_from_file_location(f"relay_e2e_{next(_n)}", BETA + "/tools/nat2/udp_reply_server.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class Sock:
    def __init__(s, port, sent): s.port, s.sent = port, sent
    def getsockname(s): return ("0.0.0.0", s.port)
    def sendto(s, data, dest): s.sent.append((s.port, bytes(data), dest))

def hdr(flags, seg, ack, size=0):
    return struct.pack("<HHHHHH", 0, 0x1234, size, 0x3040 | flags, seg, ack)

def run(new, srp_port, label):
    m = load(); gsm, srp = m.gsm, m.srp
    T = [1000.0]; m.time.monotonic = lambda: T[0]
    logs = []; m.log = lambda s: logs.append(s)
    m.SRP_BY_SESSION = new
    sent = []
    m.HOLE_SOCKS.update({p: Sock(p, sent) for p in m.RELAY_HOLES})
    m.NAT_MAIN_SOCK = Sock(45000, sent)
    jsock = Sock(3658, sent)
    H, GAMEPORT = "198.18.103.130", 12094
    def pkt(data, addr, port=45000):
        out = m.handle_packet(data, addr, T[0], port)
        for p, d in out: sent.append((port, p, d))
        return out
    def tick(dt=1.0):
        T[0] += dt; m.hole_steering(T[0]); m.hole_keepalive(T[0])
        if m.RELAY_HOSTS: m.link_silence(T[0]); m.leave_expire(T[0])
    # 1. login: SYN, 4 NAT requests, ping of socket 1
    pkt(hdr(2, 0, 0, 8) + struct.pack("<HHHH", 10, 0x1234, 0x55, 536), (H, srp_port))
    body = b""
    for s in "1234":
        h = gsm.GSMessageHeader.from_params(gsm.PROPERTY.GS, 0, gsm.MESSAGE_TYPE.NAT, gsm.SENDER_RECEIVER.P, gsm.SENDER_RECEIVER.S)
        dl = m.gsm.List(["1", [s, "SNAKE", "SPLINTERCELL3PS2US", "SC3PS2US1.0"]])
        r = gsm.GSMResponse(gsm.Message(b"", header=h, dl=dl)); r.dl = dl
        r.header.property = gsm.PROPERTY.GS; r.header.type = gsm.MESSAGE_TYPE.NAT
        body += bytes(r)
    pkt(hdr(0, 0, 0, len(body)) + body, (H, srp_port))
    clt = next(c for c in m.CLIENTS if c.addr == H)
    assert len(clt.reqs) == 4, clt.reqs
    pid1 = next(p for p, r in clt.reqs.items() if str(r["sock"]) == "1")
    pkt(struct.pack(">I", pid1), (H, GAMEPORT))                      # socket 1 ping -> ADDRESS, multi-hole armed
    tick()                                                             # steer #1 -> 45001
    def host_ping_target():
        """The console pings whatever hole the last PORT_ID named (only if the relay still steers it)."""
        c = next((c for c in m.CLIENTS if c.addr == H and getattr(c, "hole_msg", None) is not None), None)
        ids = {r["port"]: p for c2 in m.CLIENTS if c2.addr == H for p, r in c2.reqs.items() if r.get("extra")}
        return c, ids
    def console_ping(port, pid):
        pkt(struct.pack(">I", pid), (H, GAMEPORT), port)
    c, ids = host_ping_target(); console_ping(45001, ids[45001])
    pkt(hdr(4, 0xFFFF, 5), (H, srp_port))                              # console acks the steer over SRP
    res = {}
    # 2. three joiners, 30 s apart; after each, the relay should steer the console to a spare hole
    joiners = [("198.51.100.205", 10072, "DORIAN_D"), ("203.0.113.24", 3576, "StandaloneF0X"), ("198.18.133.242", 62937, "Wickedness")]
    last_target = {45001: ids[45001]}
    for i, (ip, port, user) in enumerate(joiners):
        m.relay_control(json.dumps({"op": "map", "joiner": ip, "host": H, "room": "104", "user": user}).encode(), ("127.0.0.1", 1))
        for k in range(8):
            m.relay_from_joiner(jsock, b"\x07\x00joiner-data", (ip, port), T[0]); T[0] += 0.2
        hole = m.RELAY_HOSTS[H]["src"][(ip, port)]
        opened = hole in last_target                                    # host NAT lets the relay in only on pinged holes
        res[user] = (hole, opened)
        if opened:                                                      # host answers on that hole
            pkt(b"\x05\x00host-reply", (H, GAMEPORT), hole)
        for _ in range(3): tick()
        pkt(hdr(4, 0xFFFF, 6 + i), (H, srp_port))                       # console's SRP ack of the new steer (as 23:21:58.6)
        for _ in range(25):
            tick()
            for (jip, jport, _u) in joiners[:i + 1]:                    # live links keep talking
                h2 = m.RELAY_HOSTS[H]["src"].get((jip, jport))
                if h2 is not None:
                    m.relay_from_joiner(jsock, b"\x07\x00joiner-data", (jip, jport), T[0])
                    if h2 in last_target: pkt(b"\x05\x00host-reply", (H, GAMEPORT), h2)
        c, ids = host_ping_target()                                     # the console pings its current target
        if c is not None and c.hole_target in ids:
            console_ping(c.hole_target, ids[c.hole_target]); last_target[c.hole_target] = ids[c.hole_target]
        elif c is None:                                                 # session gone: the console still pings, relay ignores it
            pkt(struct.pack(">I", 0x265146dc), (H, GAMEPORT), 45002)
    session = any(c.addr == H and c.port == srp_port for c in m.CLIENTS)
    stray = sum("dropped stray" in l for l in logs); unk = sum("unknown ping id" in l for l in logs)
    steers = sum(l.startswith("HOLE steer") for l in logs)
    # 3. logout: FIN on the session port removes the client (and is not forwarded to a joiner as game data)
    before = len(sent); pkt(hdr(1 | 8, 0xFFFF, 9), (H, srp_port))
    fin_fwd = any(d[0] != H for _, _, d in sent[before:])
    gone = not any(c.addr == H and c.port == srp_port for c in m.CLIENTS)
    print(f"{label}: session kept={session} steers={steers} 'dropped stray'={stray} 'unknown ping'={unk}")
    for u, (hole, opened) in res.items():
        print(f"    {u:14s} hole {hole}  {'host reachable (hole opened by its ping)' if opened else 'HOLE NEVER OPENED -> join fails'}")
    print(f"    logout FIN: client removed={gone}, forwarded to a joiner={fin_fwd}")
    return m, logs

if __name__ == "__main__":
    run(False, 4465, "fix off, host SRP port 4465 (router rewrote it)")
    run(True, 4465, "fix on,  host SRP port 4465 (router rewrote it)")
    run(False, 10070, "fix off, host SRP port 10070 (PS2 on modem / forwarded PC)")
    run(True, 10070, "fix on,  host SRP port 10070 (PS2 on modem / forwarded PC)")
