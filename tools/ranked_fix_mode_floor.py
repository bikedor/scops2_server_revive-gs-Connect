#!/usr/bin/env python3
"""Give back points a loss should have cost but didn't (one-time correction, 2026-11-25).

SCCT_RANKED_MODE_FLOOR (2026-11-23) tracks a separate point ledger per mode and side (e.g. NEU_SPY_SCORE)
so the Game Statistics screen never shows a tab going negative. The bug: the point change computed FOR
that one tab (floored at 0, or partly floored if the tab held fewer points than a loss costs) was then
applied to every other ledger too - overall Spy/Mercenary score, the mode-only score, and GEN_SCORE -
instead of each of those being floored at its OWN value. So an established player who lost in a mode and
side they hadn't lost in before (or had few points in) kept some or all of the points that loss should
have cost, everywhere, not just on that one tab. Confirmed on the real 2026-11-24 Mount Hospital/Cinema/
Warehouse session (BLACKNIGHT and TALLY_WHACKER both recorded a real loss with 0 points taken) and in the
2026-11-22/23 session (COOLDOR, DORIAN_D, LINEBACKER, ANDROID-DOR). Wins are never affected (the floor
only ever reduces a negative change, so a win's delta is always applied in full as before).

This tool finds every recorded Ranking match in the router log where a player genuinely lost (some other
player in the same match won - a "nobody won" match is ranked_undo_aborts.py's, not this one) but the
server applied less than a full loss, and takes the missing amount from GEN_SCORE, the overall Spy/
Mercenary score and the mode-only score (never the mode+side tab itself - that one was already correct).
Each ledger is floored at 0 on its own current value, same as the live server does, and matches are
replayed in order so later corrections see the effect of earlier ones.

Default is a dry run that only prints what it would change. --apply writes state/ladder_stats.json after
saving a copy as state/ladder_stats.json.bak-<date-time>, and remembers every match it fixed in
state/ladder_floor_fixes.json, so running it again never takes the same points twice. Run it between
sessions. Logs predating the SCCT_RANKED_MODE_FLOOR fix (tools/ubigs_router_wm_server.py, 2026-11-25) can
still be fed in safely: a match is touched only if it shows a real shortfall.

Usage:
  python tools/ranked_fix_mode_floor.py                 dry run over logs/router_wm_40005.log
  python tools/ranked_fix_mode_floor.py --apply         write the changes
  python tools/ranked_fix_mode_floor.py some.log --apply
"""
from __future__ import annotations

import argparse
import ast
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESTART = "Ubisoft GS Router WM listening"
TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\] (.*)$")
R_AUTO = re.compile(r"RANKED_FLOW mode auto-detected: (\w+)")
R_FINISHED = re.compile(r"RANKED_FLOW match (\d+) finished: (\[.*\])$")
R_KIND_END = re.compile(r"ROOM_KIND room \S+ match (\d+) ended: (?:RANKING|FREE) (\w+)")
R_RECORDED = re.compile(r"RANKED_FLOW match (\d+) recorded in ladder_stats\.json: (\[.*\])$")
NAMES = {"NEU": "Neutralisation", "SAB": "Sabotage", "EXT": "Extraction"}


def _int(x, d=0):
    try:
        return int(str(x))
    except Exception:
        return d


def find_shortfalls(paths, loss=25):
    """Recorded matches with a real winner where a loser was given less than a full loss. Returns dicts:
    key, when, mid, mode, players [(name, team, shortfall)] (shortfall is the extra amount still owed, <= 0)."""
    out = []
    run = ""
    mode = None
    rows = {}
    for p in paths:
        for line in open(p, encoding="utf-8", errors="replace"):
            m = TS.match(line.rstrip("\r\n"))
            if not m:
                continue
            ts, msg = m.group(1), m.group(2)
            if RESTART in msg:
                run, mode, rows = ts, None, {}
            elif (r := R_AUTO.search(msg)):
                mode = r.group(1)
            elif (r := R_FINISHED.search(msg)):
                try:
                    fin = ast.literal_eval(r.group(2))
                    rows[r.group(1)] = [mode, {n: a for n, a in fin[2]} if fin[1] == "38" else {}]
                except Exception:
                    rows[r.group(1)] = [mode, {}]
                mode = None                     # the "auto-detected" line belongs to this match only
            elif (r := R_KIND_END.search(msg)):
                if r.group(1) in rows and rows[r.group(1)][0] is None and r.group(2) in NAMES:
                    rows[r.group(1)][0] = r.group(2)
            elif (r := R_RECORDED.search(msg)):
                mid = r.group(1)
                try:
                    rec = ast.literal_eval(r.group(2))
                except Exception:
                    continue
                md, arrs = rows.get(mid, [None, {}])
                if not arrs or not any(_int(a[1]) > 0 for a in arrs.values()):
                    continue                    # nobody won: ranked_undo_aborts.py's match, not this one
                pl = []
                for n, applied in rec:
                    a = arrs.get(n, ["?", "0", "0", "0"])
                    res = _int(a[1])
                    if res >= 0:
                        continue                # a win is never shortchanged by this bug; nothing to check
                    owed = -loss
                    applied = int(applied)
                    shortfall = owed - applied  # how much MORE still needs to come off (<=0), 0 if already correct
                    if shortfall < 0:
                        pl.append((n, str(a[0]), shortfall))
                if pl:
                    out.append({"key": f"{run}|match {mid}|{ts}", "when": ts, "mid": mid, "mode": md, "players": pl})
    return out


def fix(db, sf, ledger_side=False):
    """Apply one match's shortfalls to db (never touches the mode+side ledger - that one was already right).
    Returns readable change lines."""
    lines = []
    mk = sf["mode"]
    for n, team, shortfall in sf["players"]:
        st = db.get(n)
        if st is None:
            lines.append(f"  {n}: not in ladder_stats.json, skipped")
            continue
        side = "SPY" if team == "0" else "SEN"
        keys = ["GEN", side] + ([mk] if mk and mk != "UNK" else [])
        ch = []
        for k in keys:
            fld = f"{k}_SCORE"
            if fld in st:
                old = _int(st[fld])
                st[fld] = max(0, old + shortfall)
                if st[fld] != old:
                    ch.append(f"{fld} {old}->{st[fld]}")
        who = f"{n} ({'Spy' if side == 'SPY' else 'Mercenary'})"
        lines.append(f"  {who}: {shortfall} more" + (f"; {', '.join(ch)}" if ch else "; already at 0, nothing to take"))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--loss", type=int, default=25, help="points a loss costs (default 25, matches SCCT_RANKED_LOSS)")
    ap.add_argument("--apply", action="store_true", help="write state/ladder_stats.json (default: dry run)")
    a = ap.parse_args(argv)
    paths = [pathlib.Path(p) for p in a.logs] or [ROOT / "logs" / "router_wm_40005.log"]
    spath = ROOT / "state" / "ladder_stats.json"
    fpath = ROOT / "state" / "ladder_floor_fixes.json"
    if not spath.exists():
        print(f"{spath} not found: nothing to correct.")
        return 1
    db = json.loads(spath.read_text(encoding="utf-8-sig"))
    done = json.loads(fpath.read_text(encoding="utf-8-sig")) if fpath.exists() else {}
    shortfalls = find_shortfalls(paths, loss=a.loss)
    todo = [x for x in shortfalls if x["key"] not in done]
    print(f"Matches with a real loss that took less than {a.loss}: {len(shortfalls)}, already fixed: {len(shortfalls) - len(todo)}")
    if not todo:
        print("Nothing to do.")
        return 0
    for sf in todo:
        print(f"\n{sf['when']}  match {sf['mid']}  {NAMES.get(sf['mode'], sf['mode'] or 'unknown mode')}")
        for ln in fix(db, sf):
            print(ln)
    if not a.apply:
        print("\nDry run: nothing written. Run again with --apply (between sessions) to write these changes.")
        return 0
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = spath.with_name(f"ladder_stats.json.bak-{stamp}")
    if spath.exists():
        bak.write_bytes(spath.read_bytes())
    spath.write_text(json.dumps(db, indent=1), encoding="utf-8")
    for sf in todo:
        done[sf["key"]] = {"fixed": stamp, "players": [[n, shortfall] for n, _, shortfall in sf["players"]]}
    fpath.write_text(json.dumps(done, indent=1), encoding="utf-8")
    print(f"\nWritten. Copy of the old file: {bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
