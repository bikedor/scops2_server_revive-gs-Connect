"""Per-mode Ranking tabs (2026-11-20): replay a session through the real lobby code and save the replies, every Ranking
screen answer (4 tabs x Overall/Spy/Mercenary x Top Rank/Find Me/my stats) and the ladder file.
Run it on a COPY of the repo (it rewrites <root>/state): python ranking_tabs_check.py <copy> off a.json, then on b.json,
and on a copy holding the previous ubigs_router_wm_server.py for the old version; compare the JSON files.
Session: Ranking Sabotage (2 matches, sides swapped), switched to Free (1 match, not recorded), Ranking Extraction,
Ranking Neutralisation, Ranking Sabotage again with the older player losing as a Spy; 4 players; one older player
with overall points only. Modes: off, on (per-mode tabs), full (on + my-stats per tab + floor at 0 per mode/side),
abort (full + a host-abort match and a match with a leaver), winner (abort + SCCT_RANKED_REQUIRE_WINNER=1)."""
import sys, os, io, types, inspect, time, json, pathlib
root, mode, outp = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path[:0] = [os.path.join(root, "tools"), os.path.join(root, "ubi-gs-new"), os.path.join(root, "pylib")]
os.environ["SCCT_LADDER_MODULE"] = "1"
os.environ["SCCT_RANKED_REQUIRE_RANKED"] = "1"
os.environ["SCCT_RANKED_MODE_BITS"] = "1"
if mode in ("on", "full", "abort", "winner"):
    os.environ["SCCT_RANKED_MODE_TABS"] = "1"
    os.environ["SCCT_RANKED_MODE_SIDE_LEDGER"] = "1"
if mode in ("full", "abort", "winner"):  # 2026-11-23: my-stats per tab + floor at 0 per mode and side
    os.environ["SCCT_RANKED_MYSTATS_TABS"] = "1"
    os.environ["SCCT_RANKED_MODE_FLOOR"] = "1"
if mode == "winner":                    # 2026-11-23: a match where nobody won (host back to lobby) is not recorded
    os.environ["SCCT_RANKED_REQUIRE_WINNER"] = "1"
st = pathlib.Path(root) / "state"
st.mkdir(exist_ok=True)
for f in st.glob("*"):
    f.unlink()
(st / "ladder_stats.json").write_text(json.dumps({"OLDTIMER": {"GEN_SCORE": 300, "SPY_SCORE": 200, "SEN_SCORE": 100,
                                                               "GEN_GAMES": 6, "GEN_WON": 4, "SPY_GAMES": 3, "SEN_GAMES": 3}}))
import ubigs_router_wm_server as M
import gsm
M._build_lobby_msg_frame = lambda dl, b5=0x24: b"x"


class _Resp:                      # stand-in for gsm.GSMResponse: keeps the reply payload only
    def __init__(s, req):
        s.header = types.SimpleNamespace()
        s.dl = None


gsm.GSMResponse = _Resp
M._lobby_reply = lambda msg, sub, pl: types.SimpleNamespace(dl=["38", [str(sub), pl]])


class Room:
    real_ip_addr = ""

    def __init__(s, gid, b0):
        s.group_id = gid
        s.info = bytes([b0]) + bytes.fromhex("682c20ff494e4930310000") + b"x" * 40
        s.config = 0x1d8

    def to_list(s):
        return [s.group_id]


rooms = []
M._lobby_state.get_rooms = lambda: rooms
M._lobby_state.rooms = {}
M._lobby_state._save_rooms_to_file = lambda: None
M._lobby_state.get_ctx = lambda k: None


class Conn:
    def sendall(s, b):
        pass


M._lobby_state.sessions = {("1.1.1.1", 1): {"conn": Conn(), "username": "A"}}
out = io.StringIO()


class Clt:
    username = "DORIAN_D"
    addr = ("1.1.1.1", 1)


def norm(x):
    x = getattr(x, "lst", x)
    if isinstance(x, (list, tuple)):
        return [norm(i) for i in x]
    if isinstance(x, (bytes, bytearray)):
        return bytes(x).hex()
    return str(x)


def call(mtype, lst):
    msg = types.SimpleNamespace(header=types.SimpleNamespace(type=mtype, property=gsm.PROPERTY.GS), dl=types.SimpleNamespace(lst=lst))
    kw = {}
    for n, p in inspect.signature(M.handle_message).parameters.items():
        if n in ("clt", "msg") or p.default is not inspect.Parameter.empty:
            continue
        kw[n] = None
    kw.update(log_fp=out, frame_bytes=b"", proxy_addr=("127.0.0.1", 1), post_ke2_replay_frames=[])
    try:
        r = M.handle_message(Clt(), msg, **kw)
    except Exception as e:
        return ["EXC", repr(e)]
    dl = getattr(r, "dl", None)
    return norm(dl) if dl is not None else repr(r)


LOBBY = gsm.MESSAGE_TYPE.LOBBY_MSG
PH = gsm.MESSAGE_TYPE.PROXY_HANDLER
replies = []


def match(rid, results):   # name -> (team '0' spy / '1' merc, result 1 win / -1 loss)
    replies.append(call(LOBBY, ["17", [rid, "3"]]))
    mid = list(M._lobby_state.matches.values())[-1]["id"]
    for n, (team, res) in results.items():
        replies.append(call(LOBBY, ["30", [str(mid), "0", [n, "3", "4", "15", [team, str(res), "1", "300"]]]]))
    replies.append(call(LOBBY, ["45", [rid]]))
    time.sleep(0.4)


win_spy = {"DORIAN_D": ("0", 1), "SNAKE": ("0", 1), "IY0": ("1", -1), "COOLDOR": ("1", -1)}
swap = {"DORIAN_D": ("1", -1), "SNAKE": ("1", 1), "IY0": ("0", 1), "COOLDOR": ("0", -1)}
r = Room("100", 0x0e)
rooms.append(r)
M._lobby_state.rooms["100"] = {"room": r}
match("100", win_spy)                                                               # Ranking Sabotage
match("100", swap)                                                                  # revenge
replies.append(call(LOBBY, ["31", ["100", "64", bytes([0x0a]) + r.info[1:]]]))       # -> Free Sabotage
match("100", win_spy)                                                               # not recorded
replies.append(call(LOBBY, ["31", ["100", "64", bytes([0x0d]) + r.info[1:]]]))       # -> Ranking Extraction
match("100", swap)
replies.append(call(LOBBY, ["31", ["100", "64", bytes([0x0f]) + r.info[1:]]]))       # -> Ranking Neutralisation
match("100", win_spy)
replies.append(call(LOBBY, ["31", ["100", "64", bytes([0x0e]) + r.info[1:]]]))       # -> Ranking Sabotage again
# near-0 case: OLDTIMER has 200 overall Spy points but none in Sabotage, and loses as a Spy
match("100", {"OLDTIMER": ("0", -1), "DORIAN_D": ("0", -1), "SNAKE": ("1", 1), "IY0": ("1", 1)})
if mode in ("abort", "winner"):
    # host returned everyone to the lobby: all four sent as lost (2026-11-23 Cinema test)
    match("100", {"DORIAN_D": ("0", -1), "SNAKE": ("0", -1), "IY0": ("1", -1), "COOLDOR": ("1", -1)})
    # one Mercenary quit mid-match: he stays in the list as a loss, the Spies still win (2026-11-23 Station test)
    match("100", {"DORIAN_D": ("0", 1), "SNAKE": ("0", 1), "IY0": ("1", -1), "COOLDOR": ("1", -1)})
cols = {"GEN": ["ALIAS", "GLOBAL_RANK", "GEN_LEVEL", "GEN_SCORE", "GEN_GAMES", "GEN_WON", "GEN_WINRATIO", "GEN_TIME", "SPY_TIME", "SEN_TIME"],
        "SPY": ["ALIAS", "QUERY_RANK", "SPY_LEVEL", "SPY_SCORE", "SPY_GAMES", "SPY_WON", "SPY_WINRATIO", "SPY_TIME"],
        "SEN": ["ALIAS", "QUERY_RANK", "SEN_LEVEL", "SEN_SCORE", "SEN_GAMES", "SEN_WON", "SEN_WINRATIO", "SEN_TIME"]}
q = {}
rid = 100
for tab in "0123":
    for side in ("GEN", "SPY", "SEN"):
        for qt, args in (("3", ["0", "19"]), ("1", ["COOLDOR", "19"]), ("2", ["DORIAN_D"])):
            rid += 1
            req = ["1281", "2", ["1", [str(rid), "SPLINTERCELL3PS2US", "0", tab, [qt, args], [cols[side], [side + "_SCORE", "0"], []]]]]
            rr = call(PH, req)
            try:
                rows = rr[2][1][1][3]
            except Exception:
                rows = rr
            q[f"tab{tab}/{side}/type{qt}"] = rows
db = json.loads((st / "ladder_stats.json").read_text())
errs = [l for l in out.getvalue().splitlines() if "failed" in l or "error" in l.lower() or "EXC" in l]
flow = [l for l in out.getvalue().splitlines() if "RANKED_FLOW" in l]
json.dump({"replies": replies, "queries": q, "db": db, "errors": errs, "flow": flow}, open(outp, "w"), indent=0)
print("done", mode, "replies", len(replies), "queries", len(q), "errors", len(errs))
