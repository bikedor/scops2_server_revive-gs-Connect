"""Summarise the relay's SHADOW lines (SCCT_RELAY_SHADOW, stage 1, log only): per host, what the fresh / stale hole tests
found, and for every link a fresh-hole rule WOULD have sent at once, how long that link really waited (its AGE line).

    python3 tools/sim/shadow_report.py logs/udp_45000_reply.log [more logs...]
"""
import re
import sys
from collections import defaultdict

TS = re.compile(r"^\[(?P<ts>[^\]]+)\] (?P<msg>.*)$")
HOST = re.compile(r"host=(?P<host>.+?) \((?P<state>\w+)\)")
PLAYER = re.compile(r"player=(?P<player>\S+(?: \S+)?:\d+)")
AGE_WAIT = re.compile(r"outcome=(?P<out>\w+) (?:wait=(?P<wait>[\d.]+)s|after=(?P<after>\d+)s)")


def main(paths):
    tests = defaultdict(lambda: defaultdict(int))
    links = defaultdict(list)
    hints, summaries = [], []
    pending = {}                       # player -> index into links[host] of its last WOULD line
    for p in paths:
        for ln in open(p, errors="replace"):
            m = TS.match(ln.rstrip("\n"))
            if not m:
                continue
            ts, msg = m["ts"], m["msg"]
            if msg.startswith("SHADOW test"):
                h = HOST.search(msg)["host"]
                cls = "fresh" if " fresh (" in msg else "stale"
                kind = ("ok" if ": OK" in msg else "no-reply" if "NO REPLY" in msg else
                        "ambiguous" if "ambiguous" in msg else "no evidence")
                tests[h][f"{cls} {kind}"] += 1
            elif msg.startswith("SHADOW link"):
                h = HOST.search(msg)["host"]
                pl = PLAYER.search(msg)["player"]
                would = "WOULD" in msg
                links[h].append({"ts": ts, "player": pl, "would": would, "line": msg, "wait": None})
                pending[(h, pl)] = len(links[h]) - 1
            elif msg.startswith("AGE host="):
                h = HOST.search(msg)["host"]
                pl = PLAYER.search(msg)["player"]
                i = pending.pop((h, pl), None)
                if i is not None:
                    w = AGE_WAIT.search(msg)
                    links[h][i]["wait"] = (f"{w['wait']}s" if w and w["wait"] else f"never ({w['after']}s)" if w else "?")
            elif msg.startswith("SHADOW hint"):
                hints.append(f"{ts}  {msg}")
            elif msg.startswith("SHADOW summary"):
                summaries.append(f"{ts}  {msg}")
    if not tests and not links and not hints:
        print("no SHADOW lines found (is SCCT_RELAY_SHADOW on, and is this the relay log udp_45000_reply.log?)")
        return 1
    for h in sorted(set(tests) | set(links)):
        print(f"host {h}")
        t = tests.get(h, {})
        for cls in ("fresh", "stale"):
            row = ", ".join(f"{k.split(' ', 1)[1]} {v}" for k, v in sorted(t.items()) if k.startswith(cls))
            print(f"  {cls} hole tests: {row or 'none'}")
        ls = links.get(h, [])
        wl = [x for x in ls if x["would"]]
        print(f"  links on fresh/stale holes: {len(ls)}, a fresh-hole rule would have acted on {len(wl)}")
        for x in wl:
            print(f"    {x['ts']}  {x['player']:<34} really waited {x['wait'] or '(no AGE line)'}")
        print()
    if hints:
        print("UPnP hints from scopsDNS:")
        for x in hints:
            print(f"  {x}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
