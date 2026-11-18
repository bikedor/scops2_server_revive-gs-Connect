"""Summarise the relay's AGE lines (SCCT_RELAY_AGE_LOG): for each host, how long it had been silent on a hole when a link
came back there, and what happened. This is the table a cold-age value should be read from.

    python3 tools/sim/age_report.py logs/udp_45000_reply.log [more logs...]
"""
import re
import sys
from collections import defaultdict

LINE = re.compile(r"^\[(?P<ts>[^\]]+)\] AGE host=(?P<host>.+?) \((?P<state>\w+)\) player=(?P<player>.+?) kind=(?P<kind>\w+)"
                  r"(?: quiet=(?P<quiet>\d+)s)? hole=(?P<hole>\w+)"
                  r"(?: host_data=(?P<data>\w+?)s? host_any=(?P<any>\w+?)s? cold=(?P<cold>\w+))?"
                  r" ctx=(?P<ctx>[\w-]+) outcome=(?P<out>\w+) (?:wait=(?P<wait>[\d.]+)s|after=(?P<after>\d+)s)(?P<rest>.*)$")
BUCKETS = [(0, 15), (15, 30), (30, 40), (40, 50), (50, 60), (60, 90), (90, 180), (180, 10 ** 9)]


def bucket(v):
    for lo, hi in BUCKETS:
        if lo <= v < hi:
            return f"{lo}-{hi}s" if hi < 10 ** 9 else f"{lo}s+"
    return "?"


def main(paths):
    recs = []
    for p in paths:
        for ln in open(p, errors="replace"):
            m = LINE.match(ln.rstrip("\n"))
            if m:
                recs.append(m.groupdict())
    if not recs:
        print("no AGE lines found (is SCCT_RELAY_AGE_LOG on, and is this the relay log udp_45000_reply.log?)")
        return 1
    print(f"{len(recs)} AGE lines\n")
    by_host = defaultdict(list)
    for r in recs:
        by_host[f"{r['host']} ({r['state']})"].append(r)
    for host, rs in by_host.items():
        print(f"host {host}: {len(rs)} link starts/returns")
        print("  host silence on the hole (game data) | links | fast <2s | slow 2-25s | late >25s | never | went via a hold/move")
        rows = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
        for r in rs:
            d = r["data"]
            key = "never used" if d in (None, "never") else bucket(int(d))
            row = rows[key]
            row[0] += 1
            if r["out"] == "answered":
                w = float(r["wait"])
                row[1 if w < 2 else 2 if w <= 25 else 3] += 1
            else:
                row[4] += 1
            if "steps=" in (r["rest"] or ""):
                row[5] += 1
        order = [f"{lo}-{hi}s" if hi < 10 ** 9 else f"{lo}s+" for lo, hi in BUCKETS] + ["never used"]
        for k in order:
            if k in rows:
                n, fast, slow, late, never, steps = rows[k]
                print(f"  {k:>36} | {n:5d} | {fast:8d} | {slow:10d} | {late:9d} | {never:5d} | {steps}")
        bad = [r for r in rs if r["out"] != "answered" or float(r["wait"] or 0) > 25]
        if bad:
            print("  late or never answered:")
            for r in bad:
                w = f"wait {r['wait']}s" if r["wait"] else f"never ({r['after']}s)"
                print(f"    {r['ts']}  {r['player']:<34} {r['kind']:<6} hole {r['hole']} host_data={r['data']} "
                      f"host_any={r['any']} cold={r['cold']} ctx={r['ctx']} -> {w}{r['rest']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
