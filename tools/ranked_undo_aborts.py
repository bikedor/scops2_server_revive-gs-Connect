#!/usr/bin/env python3
"""Give back the points taken by aborted Ranking matches (one-time correction, 2026-11-23).

When the host returns everyone to the lobby mid-match, every console sends all players as lost and, before
SCCT_RANKED_REQUIRE_WINNER=1, the server recorded that as a loss for everyone. This tool finds those matches in
the router log (recorded, and nobody won) and undoes exactly what was written for them: the points that were
applied (a loss at 0 took nothing, so nothing is given back), one game and the time played, in every total the
match touched (side, mode, mode and side, overall). Wins are not touched (nobody won).

Default is a dry run that only prints what it would change. --apply writes state/ladder_stats.json after saving
a copy as state/ladder_stats.json.bak-<date-time>, and remembers every match it undid in state/ladder_fixes.json,
so running it again never gives the same points back twice. Run it between sessions.

Usage:
  python tools/ranked_undo_aborts.py                 dry run over logs/router_wm_40005.log
  python tools/ranked_undo_aborts.py --apply         write the changes
  python tools/ranked_undo_aborts.py some.log --apply
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


def find_aborts(paths):
    """Recorded matches where no player won. Returns dicts: key, when, mid, mode, players [(name, team, applied, time)]."""
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
                # a mode fixed in state/ranked_mode.txt logs no "auto-detected" line: use the room's mode instead
                if r.group(1) in rows and rows[r.group(1)][0] is None and r.group(2) in NAMES:
                    rows[r.group(1)][0] = r.group(2)
            elif (r := R_RECORDED.search(msg)):
                mid = r.group(1)
                try:
                    rec = ast.literal_eval(r.group(2))
                except Exception:
                    continue
                md, arrs = rows.get(mid, [None, {}])
                if not arrs or any(_int(a[1]) > 0 for a in arrs.values()):
                    continue                    # someone won: a real match
                if not any(_int(a[1]) < 0 for a in arrs.values()):
                    continue
                pl = []
                for n, applied in rec:
                    a = arrs.get(n, ["?", "0", "0", "0"])
                    applied = int(applied)
                    # the change the server really applied is in the reply row (arr[7], "Game Results"); older versions
                    # logged the nominal -25 even for a player already at 0. Tab-test numbers (positive) are ignored.
                    a7 = _int(a[7], 1) if len(a) > 8 and str(a[8]) == "1" else 1
                    if applied <= a7 <= 0:
                        applied = a7
                    if applied > 0:
                        applied = 0                 # nobody won: never take points away
                    pl.append((n, str(a[0]), applied, max(0, _int(a[3]))))
                out.append({"key": f"{run}|match {mid}|{ts}", "when": ts, "mid": mid, "mode": md, "players": pl})
    return out


def undo(db, ab, ledger_side=True):
    """Undo one aborted match in db; returns readable change lines."""
    lines = []
    mk = ab["mode"]
    for n, team, applied, t in ab["players"]:
        st = db.get(n)
        if st is None:
            lines.append(f"  {n}: not in ladder_stats.json, skipped")
            continue
        side = "SPY" if team == "0" else "SEN"
        keys = ["GEN", side]
        if mk and mk != "UNK":
            keys.append(mk)
            if ledger_side and f"{mk}_{side}_GAMES" in st:
                keys.append(f"{mk}_{side}")
        ch = []
        for k in keys:
            if k != "GEN" and f"{k}_SCORE" in st and applied:
                old = _int(st[f"{k}_SCORE"])
                st[f"{k}_SCORE"] = max(0, old - applied)
                ch.append(f"{k}_SCORE {old}->{st[k + '_SCORE']}")
            if f"{k}_GAMES" in st:
                st[f"{k}_GAMES"] = max(0, _int(st[f"{k}_GAMES"]) - 1)
            if f"{k}_TIME" in st:
                st[f"{k}_TIME"] = max(0, _int(st[f"{k}_TIME"]) - t)
        st["GEN_SCORE"] = _int(st.get("SPY_SCORE", 0)) + _int(st.get("SEN_SCORE", 0))
        who = f"{n} ({'Spy' if side == 'SPY' else 'Mercenary'})"
        lines.append(f"  {who}: {'+' + str(-applied) if applied else 'no points to give back'}"
                     f"; one game and {t}s removed" + (f"; {', '.join(ch)}" if ch else ""))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--apply", action="store_true", help="write state/ladder_stats.json (default: dry run)")
    a = ap.parse_args(argv)
    paths = [pathlib.Path(p) for p in a.logs] or [ROOT / "logs" / "router_wm_40005.log"]
    spath = ROOT / "state" / "ladder_stats.json"
    fpath = ROOT / "state" / "ladder_fixes.json"
    if not spath.exists():
        print(f"{spath} not found: nothing to correct.")
        return 1
    db = json.loads(spath.read_text(encoding="utf-8-sig"))
    done = json.loads(fpath.read_text(encoding="utf-8-sig")) if fpath.exists() else {}
    aborts = find_aborts(paths)
    todo = [x for x in aborts if x["key"] not in done]
    print(f"Aborted Ranking matches in the log (recorded, nobody won): {len(aborts)}, already given back: {len(aborts) - len(todo)}")
    if not todo:
        print("Nothing to do.")
        return 0
    for ab in todo:
        print(f"\n{ab['when']}  match {ab['mid']}  {NAMES.get(ab['mode'], ab['mode'] or 'unknown mode')}")
        for ln in undo(db, ab):
            print(ln)
    if not a.apply:
        print("\nDry run: nothing written. Run again with --apply (between sessions) to write these changes.")
        return 0
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = spath.with_name(f"ladder_stats.json.bak-{stamp}")
    if spath.exists():
        bak.write_bytes(spath.read_bytes())
    spath.write_text(json.dumps(db, indent=1), encoding="utf-8")
    for ab in todo:
        done[ab["key"]] = {"undone": stamp, "players": [[n, applied] for n, _, applied, _ in ab["players"]]}
    fpath.write_text(json.dumps(done, indent=1), encoding="utf-8")
    print(f"\nWritten. Copy of the old file: {bak.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
