"""Room kind check (log only): free rooms and ranked rooms get the right ROOM_KIND lines.

Run from the repo root:  python tools/sim/room_kind_check.py .
It drives the lobby server's match flow (create label, room update 31, start 17, scores 30, finish 45) with fake
messages and checks the log: the Game Type and mode come from the room info's first byte (bit 0x04 Ranking, low
2 bits 3 NEU / 2 SAB / 1 EXT), a switch Ranking -> Free is logged, a Free room that still sends scores (STRIDERSTROKE,
2026-11-18/19) is flagged, SCCT_RANKED_REQUIRE_RANKED=1 skips recording it, SCCT_RANKED_MODE_BITS=1 records a Free
Sabotage room as SAB instead of UNK, and the replies to the consoles (38 with results, 39 without) stay the same.
Exit code 0 = pass.
"""
import sys, os, io, inspect, types, time, tempfile, pathlib
root = sys.argv[1]
sys.path[:0] = [os.path.join(root, "tools"), os.path.join(root, "ubi-gs-new")]
os.chdir(tempfile.mkdtemp())
for _m in ("rsa","blowfish"):
    try: __import__(_m)
    except ImportError:
        _mm = types.ModuleType(_m)
        class _Any:
            def __init__(s, *a, **k): pass
            def __getattr__(s, n): return _Any()
            def __call__(s, *a, **k): return _Any()
        _mm.__getattr__ = lambda n: _Any
        sys.modules[_m] = _mm
import ubigs_router_wm_server as M
import gsm
M._lobby_reply = lambda msg, sub, pl: ("reply", sub, pl)
M._build_lobby_msg_frame = lambda dl, b5=0x24: b"x"
class Room:
    def __init__(s, gid, info): s.group_id = gid; s.info = info; s.config = 0x1d8
rooms = []
M._lobby_state.get_rooms = lambda: rooms
M._lobby_state.get_ctx = lambda k: None
class Conn:
    def sendall(s, b): pass
M._lobby_state.sessions = {("1.1.1.1", 1): {"conn": Conn(), "username": "A"}}
out = io.StringIO()
class Clt: username = "HOST"; addr = ("1.1.1.1", 1)
def send(sub, payload):
    msg = types.SimpleNamespace(header=types.SimpleNamespace(type=gsm.MESSAGE_TYPE.LOBBY_MSG), dl=types.SimpleNamespace(lst=[str(sub), payload]))
    params = inspect.signature(M.handle_message).parameters
    kw = {}
    for n, p in params.items():
        if n in ("clt", "msg"): continue
        if p.default is not inspect.Parameter.empty: continue
        kw[n] = None
    kw.update(log_fp=out, frame_bytes=b"", proxy_addr=("127.0.0.1", 1), post_ke2_replay_frames=[])
    try:
        return M.handle_message(Clt(), msg, **kw)
    except Exception as e:
        return ("EXC", repr(e))
class Room2(Room):
    real_ip_addr = ""
def room(gid, b0):
    r = Room2(gid, bytes([b0]) + bytes.fromhex("a82140ff494e4930310000") + b"x" * 40)
    rooms.append(r); M._lobby_state.rooms[gid] = {"room": r}; return r
M._lobby_state.rooms = {}
M._lobby_state._save_rooms_to_file = lambda: None
kinds = M._lobby_state.__dict__.setdefault("room_kind", {})
def match(rid, scores):
    send(17, [rid, "3"])
    mid = [m for m in M._lobby_state.matches.values() if True][-1]["id"]
    for k in range(scores):
        send(30, [str(mid), "0", [f"P{k}", "3", "4", "15", ["1", "1", "1", "100"]]])
    send(45, [rid])
os.environ.pop("SCCT_RANKED_REQUIRE_RANKED", None); os.environ.pop("SCCT_RANKED_MODE_BITS", None)
room("107", 0x13); kinds["107:created"] = "FREE NEU (byte 0x13)"      # SNAKE: Free all session, no scores
match("107", 0)
room("106", 0x0f); kinds["106:created"] = "RANKING NEU (byte 0x0f)"   # IY0: Ranking all session, scores
match("106", 1); match("106", 0)
room("108", 0x0f); kinds["108:created"] = "RANKING NEU (byte 0x0f)"   # STRIDERSTROKE: created Ranking, switched to Free
send(31, ["108", "64", bytes([0x0b]) + bytes.fromhex("a82140ff494e4930310000") + b"x" * 40])
match("108", 1)
os.environ["SCCT_RANKED_REQUIRE_RANKED"] = "1"
match("108", 1)
os.environ.pop("SCCT_RANKED_REQUIRE_RANKED")
room("109", 0x0e); kinds["109:created"] = "RANKING SAB (byte 0x0e)"   # SAB: whole-byte table already right
send(31, ["109", "64", bytes([0x0a]) + bytes.fromhex("a82140ff494e4930310000") + b"x" * 40])   # Free SAB: table says UNK
match("109", 1)
os.environ["SCCT_RANKED_MODE_BITS"] = "1"
match("109", 1)
os.environ.pop("SCCT_RANKED_MODE_BITS")
time.sleep(0.6)
log = out.getvalue()
kind = [l[26:] for l in log.splitlines() if "ROOM_KIND" in l or "mode " in l or "not recorded (room" in l or "would skip" in l]
for l in kind: print(l[:200])
checks = [
    "ROOM_KIND room 107 match 1 starts: FREE NEU (byte 0x13); room created as FREE NEU",
    "ROOM_KIND room 107 match 1 ended: FREE NEU (byte 0x13) - no scores, nothing to record; normal for a Free room",
    "ROOM_KIND room 106 match 2 starts: RANKING NEU (byte 0x0f)",
    "ROOM_KIND room 106 match 2 ended: RANKING NEU (byte 0x0f) - scores from 1 player(s)",
    "ROOM_KIND room 106 match 3 ended: RANKING NEU (byte 0x0f) - no scores this match",
    "ROOM_KIND room 108 switched: RANKING NEU (byte 0x0f) -> FREE NEU (byte 0x0b)",
    "ROOM_KIND room 108 match 4 ended: FREE NEU (byte 0x0b) - scores were sent although the room was Free at match start (room created as RANKING NEU",
    "RANKED_FLOW match 4 room was Free at match start but scores were sent; recorded as before (SCCT_RANKED_REQUIRE_RANKED=1 would skip it)",
    "RANKED_FLOW match 5 not recorded (room was set to Free when the match started: FREE NEU (byte 0x0b))",
    "ROOM_KIND room 109 switched: RANKING SAB (byte 0x0e) -> FREE SAB (byte 0x0a)",
    "RANKED_FLOW mode by bits would be SAB (SCCT_RANKED_MODE_BITS=1); recording as UNK",
    "RANKED_FLOW mode auto-detected: SAB",
    "RANKED_FLOW match 1 finished: ['1', '39', ['0']]",
    "RANKED_FLOW match 2 finished: ['2', '38',",
    "RANKED_FLOW match 5 finished: ['5', '38',",
]
bad = [c for c in checks if c not in log]
if "ROOM_KIND failed" in log:
    bad.append("a ROOM_KIND failed line")
print("PASS" if not bad else "FAIL: missing " + repr(bad))
sys.exit(1 if bad else 0)
