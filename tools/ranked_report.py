#!/usr/bin/env python3
"""Ranked match report: one block per match from the router log, with flags for anything that needs a look.

Reads only the log (never the server or state/), so it is safe to run while the server is up.
For each match: time, room, map, Ranking/Free and mode at start, the mode it was recorded as, every player's side
(Spy/Mercenary), result, time played, points and new side total, and whether it counted.
Flags: a Free match that counted, a Ranking match that did not (and why), unknown or mismatched mode, results that
disagree (both sides won), players who left before the end, matches that never finished.

Usage:
  python tools/ranked_report.py                       logs/router_wm_40005.log, all matches
  python tools/ranked_report.py --last                only since the last server start
  python tools/ranked_report.py --since 2026-11-22    only matches from this date (or "2026-11-22 18:00")
  python tools/ranked_report.py --scored              hide Free matches that sent no scores
  python tools/ranked_report.py some.log other.log    other logs (read in the order given)
Writes the same text to logs/ranked_report.txt (or next to the first log given).
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODE_NAMES = {"NEU": "Neutralisation", "SAB": "Sabotage", "EXT": "Extraction", "UNK": "unknown mode"}
RESTART = "Ubisoft GS Router WM listening"

TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.\d+)?\] (.*)$")
R_START = re.compile(r"RANKED_FLOW match (\d+) started in room (\S+), players=(\[.*\])$")
R_INFO = re.compile(r"RANKED_FLOW MODE_INFO room=(\S+) .*info_ascii=(.*)$")
R_KIND_START = re.compile(r"ROOM_KIND room (\S+) match (\d+) starts: (RANKING|FREE) (\w+)")
R_KIND_END = re.compile(r"ROOM_KIND room (\S+) match (\d+) ended: .* - (.*)$")
R_STORED = re.compile(r"RANKED_FLOW match (\d+) results stored: (\{.*\})$")
R_BITS = re.compile(r"RANKED_FLOW mode by bits would be (\w+)")
R_AUTO = re.compile(r"RANKED_FLOW mode auto-detected: (\w+)")
R_SHORT = re.compile(r"RANKED_FLOW match (\d+) not recorded \(too short: (\d+)s < (\d+)s\)")
R_FREE_SKIP = re.compile(r"RANKED_FLOW match (\d+) not recorded \(room was set to Free")
R_NOWIN = re.compile(r"RANKED_FLOW match (\d+) not recorded \(nobody won")
R_FEW = re.compile(r"RANKED_FLOW match (\d+) not recorded \(players=(\d+), min=(\d+)\)")
R_FREE_KEPT = re.compile(r"RANKED_FLOW match (\d+) room was Free at match start but scores were sent")
R_FINISHED = re.compile(r"RANKED_FLOW match (\d+) finished: (\[.*\])$")
R_RECORDED = re.compile(r"RANKED_FLOW match (\d+) recorded in ladder_stats\.json: (\[.*\])$")
R_REC_FAIL = re.compile(r"RANKED_FLOW recording failed: (.*)$")
R_ORPHAN = re.compile(r"RANKED_FLOW (SUBMIT_MATCH for unknown match \S+|MATCH_FINISH for room \S+ with no running match)")
R_MAP = re.compile(r"([A-Z]{3}\d\d)(?:\\x00)+([^\\']+)")


def _lit(text):
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


def _int(x, default=0):
    try:
        return int(str(x))
    except Exception:
        return default


def _mmss(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class Match:
    def __init__(self, session, mid, room, start, players):
        self.session, self.id, self.room, self.start = session, mid, room, start
        self.players = players
        self.end = None
        self.map = ""
        self.kind = None          # "RANKING" / "FREE" / None (older logs without ROOM_KIND)
        self.mode_start = None
        self.values = {}          # last SUBMIT_MATCH values per player: [side, result, ?, seconds]
        self.order = []
        self.mode_rec = None      # mode the server recorded it as
        self.mode_bits = None
        self.not_recorded = None  # reason text
        self.free_kept = False
        self.final_code = None    # "38" with scores, "39" without
        self.final_rows = {}
        self.recorded = None      # list of (name, delta) when written to ladder_stats.json
        self.rec_failed = None
        self.end_note = ""
        self.unfinished = None    # reason when it never finished


def parse(paths):
    matches, orphans = [], []
    starts = [None]   # first timestamp of each server run; index = session
    session = 0
    seen = False      # any ranked line in the current run yet
    open_by_id, open_by_room = {}, {}
    pend = {}   # mode lines come before the "finished" line and carry no match id

    def close_session(why):
        for m in open_by_id.values():
            if m.end is None:
                m.unfinished = why
        open_by_id.clear()
        open_by_room.clear()

    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fp:
            for raw in fp:
                line = raw.rstrip("\r\n")
                if RESTART in line:
                    close_session("never finished: the server restarted")
                    if seen:
                        session += 1
                        starts.append(None)
                        seen = False
                    continue
                mt = TS.match(line)
                if not mt or ("RANKED_FLOW" not in line and "ROOM_KIND" not in line):
                    continue
                ts, msg = mt.group(1), mt.group(2)
                seen = True
                if starts[session] is None:
                    starts[session] = ts
                if (r := R_START.search(msg)):
                    mid, room = r.group(1), r.group(2)
                    if mid in open_by_id or any(m.session == session and m.id == mid for m in matches):
                        close_session("never finished: the log continues with a new server run")
                        session += 1
                        starts.append(ts)
                    m = Match(session, mid, room, ts, [str(p) for p in (_lit(r.group(3)) or [])])
                    matches.append(m)
                    open_by_id[mid] = m
                    open_by_room[room] = m
                    pend = {}
                elif (r := R_INFO.search(msg)):
                    m = open_by_room.get(r.group(1))
                    if m is not None and (mp := R_MAP.search(r.group(2))):
                        m.map = mp.group(2).strip()
                elif (r := R_KIND_START.search(msg)):
                    m = open_by_id.get(r.group(2))
                    if m is not None:
                        m.kind, m.mode_start = r.group(3), r.group(4)
                elif (r := R_STORED.search(msg)):
                    m = open_by_id.get(r.group(1))
                    vals = _lit(r.group(2))
                    if m is not None and isinstance(vals, dict):
                        for n, v in vals.items():
                            if n not in m.values:
                                m.order.append(n)
                            m.values[n] = [str(x) for x in v] if isinstance(v, (list, tuple)) else []
                elif (r := R_BITS.search(msg)):
                    pend["bits"] = r.group(1)
                elif (r := R_AUTO.search(msg)):
                    pend["auto"] = r.group(1)
                elif (r := R_SHORT.search(msg)):
                    pend.setdefault("why", f"too short ({r.group(2)}s, needs {r.group(3)}s)")
                elif R_NOWIN.search(msg):
                    pend.setdefault("why", NOWIN_WHY)
                elif R_FREE_SKIP.search(msg):
                    pend.setdefault("why", "the room was set to Free when the match started")
                elif (r := R_FEW.search(msg)):
                    # the server also logs this line after a "too short" or "Free" skip (players=0); the first reason wins
                    n, mn = int(r.group(2)), int(r.group(3))
                    pend.setdefault("why", f"only {n} player(s) sent scores, needs {mn}" if n < mn
                                    else "nobody won or lost in the scores sent (all results 0)")
                elif R_FREE_KEPT.search(msg):
                    pend["free_kept"] = True
                elif (r := R_FINISHED.search(msg)):
                    m = open_by_id.get(r.group(1))
                    if m is None:
                        continue
                    m.end = ts
                    fin = _lit(r.group(2))
                    if isinstance(fin, list) and len(fin) >= 3:
                        m.final_code = str(fin[1])
                        if isinstance(fin[2], list):
                            for row in fin[2]:
                                if isinstance(row, list) and len(row) == 2 and isinstance(row[1], list):
                                    m.final_rows[str(row[0])] = [str(x) for x in row[1]]
                    m.mode_rec = pend.get("auto")
                    m.mode_bits = pend.get("bits")
                    m.not_recorded = pend.get("why")
                    m.free_kept = pend.get("free_kept", False)
                    pend = {}
                elif (r := R_RECORDED.search(msg)):
                    for m in reversed(matches):
                        if m.session == session and m.id == r.group(1):
                            m.recorded = [(str(a), _int(b)) for a, b in (_lit(r.group(2)) or [])]
                            break
                elif (r := R_REC_FAIL.search(msg)):
                    for m in reversed(matches):
                        if m.session == session and m.end is not None:
                            m.rec_failed = r.group(1)
                            break
                elif (r := R_KIND_END.search(msg)):
                    m = open_by_id.pop(r.group(2), None)
                    if m is not None:
                        m.end_note = r.group(3)
                        if open_by_room.get(m.room) is m:
                            open_by_room.pop(m.room, None)
                elif (r := R_ORPHAN.search(msg)):
                    orphans.append((ts, session, r.group(1)))
    # matches whose "finished" line was seen but no ROOM_KIND "ended" line (older logs) are done, not open
    for m in open_by_id.values():
        if m.end is None:
            m.unfinished = "never finished: still running, or the log ends here"
    return matches, orphans, starts


def applied(m):
    """Points the game actually applied per player: the change in the finished row (a loss at 0 stays 0, because
    totals never go below 0), falling back to the nominal delta the server logged."""
    out = {}
    for n, d in (m.recorded or []):
        row = m.final_rows.get(n)
        if row and len(row) > 7:
            try:
                out[n] = int(row[7])
                continue
            except ValueError:
                pass
        out[n] = d
    return out


NOWIN_WHY = "nobody won (the host returned everyone to the lobby)"


def flags_for(m):
    out = []
    counted = bool(m.recorded)
    if m.unfinished:
        out.append(m.unfinished)
    if m.rec_failed:
        out.append(f"recording failed: {m.rec_failed}")
    if m.kind == "FREE" and counted:
        out.append("Free match counted toward ranking")
    if m.kind == "RANKING" and m.end and not counted:
        if m.not_recorded == NOWIN_WHY:
            pass                                  # expected: an aborted match is not counted (SCCT_RANKED_REQUIRE_WINNER=1)
        elif m.not_recorded:
            out.append(f"Ranking match not counted: {m.not_recorded}")
        elif not m.values:
            out.append("Ranking match not counted: no scores arrived (everyone left early or the round was cut short)")
        else:
            out.append("Ranking match not counted (no reason in the log)")
    if counted and (m.mode_rec in (None, "UNK")):
        out.append("recorded with an unknown mode (goes to no mode tab)")
    if m.mode_bits and m.mode_rec and m.mode_bits != m.mode_rec:
        out.append(f"mode check disagrees: recorded as {m.mode_rec}, room bits say {m.mode_bits}")
    if counted and m.mode_start and m.mode_rec and m.mode_start not in ("UNK", m.mode_rec):
        out.append(f"room said {m.mode_start} at start but it was recorded as {m.mode_rec}")
    if m.values:
        sides = {}
        for n in m.order:
            v = m.values[n]
            sides.setdefault(v[0] if v else "?", []).append(_int(v[1]) if len(v) > 1 else 0)
        for s, res in sides.items():
            if len({r for r in res}) > 1:
                out.append(f"{'Spies' if s == '0' else 'Mercenaries'} have different results ({res})")
        won = [s for s, res in sides.items() if res and all(r > 0 for r in res)]
        lost = [s for s, res in sides.items() if res and all(r < 0 for r in res)]
        if len(sides) == 2 and (len(won) == 2 or len(lost) == 2) and m.not_recorded != NOWIN_WHY:
            out.append("both sides " + ("won" if len(won) == 2 else "lost"))
        if len(sides) == 2 and len({len(v) for v in sides.values()}) > 1:
            out.append("uneven teams (" + " v ".join(f"{len(sides[s])}" for s in sorted(sides)) + ")")
        gone = [p for p in m.players if p not in m.values]
        if gone and m.end:
            out.append("no scores from (left early?): " + ", ".join(gone))
    return out


def render(m, show_scores=True):
    span = m.start[:16]
    if m.end:
        span += f" - {m.end[11:16]}"
        try:
            import datetime as dt
            d = dt.datetime.strptime(m.end, "%Y-%m-%d %H:%M:%S") - dt.datetime.strptime(m.start, "%Y-%m-%d %H:%M:%S")
            span += f" ({_mmss(d.total_seconds())})"
        except Exception:
            pass
    kind = f"{m.kind} {MODE_NAMES.get(m.mode_start, m.mode_start)}" if m.kind else "Ranking/Free not logged"
    head = f"Match {m.id}  {span}  room {m.room}  {m.map or 'map ?'}  {kind}"
    lines = [head]
    if m.recorded:
        lines.append(f"  Counted: YES, as {MODE_NAMES.get(m.mode_rec or 'UNK', m.mode_rec)}")
    elif m.end:
        why = m.not_recorded or ("no scores (normal for a Free room)" if not m.values and m.kind == "FREE"
                                 else "no scores" if not m.values else "not recorded")
        lines.append(f"  Counted: no - {why}")
    if show_scores and m.values:
        deltas = applied(m)
        lines.append(f"  {'Player':<16}{'Side':<12}{'Result':<8}{'Played':>7}{'Points':>8}{'Side total':>12}")
        for n in m.order:
            v = m.values[n] + ["0"] * 4
            side = "Spy" if v[0] == "0" else "Mercenary"
            res = _int(v[1])
            resw = "Win" if res > 0 else "Loss" if res < 0 else "-"
            pts = f"{deltas[n]:+d}" if n in deltas else "-"
            row = m.final_rows.get(n)
            tot = row[6] if (m.recorded and row and len(row) > 6) else "-"
            quit_ = "  quit mid-match" if str(v[2]) == "0" else ""
            if quit_ and row and len(row) > 18 and str(row[18]) == "1":
                quit_ += " (disconnection counted)"
            lines.append(f"  {n:<16}{side:<12}{resw:<8}{_mmss(_int(v[3])):>7}{pts:>8}{tot:>12}{quit_}")
    elif m.players:
        lines.append("  Players: " + ", ".join(m.players))
    for f in flags_for(m):
        lines.append(f"  ! {f}")
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ranked match report from the router log (read only).")
    ap.add_argument("logs", nargs="*", help="router log(s); default logs/router_wm_40005.log")
    ap.add_argument("--last", action="store_true", help="only since the last server start")
    ap.add_argument("--since", help="only matches from this date/time, e.g. 2026-11-22 or '2026-11-22 18:00'")
    ap.add_argument("--scored", action="store_true", help="hide Free matches that sent no scores")
    ap.add_argument("--out", help="also write the report here (default: ranked_report.txt next to the log)")
    a = ap.parse_args(argv)

    paths = [pathlib.Path(p) for p in a.logs] or [ROOT / "logs" / "router_wm_40005.log"]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print("Log not found: " + ", ".join(missing))
        return 1
    matches, orphans, starts = parse(paths)
    if a.last and matches:
        last = max(m.session for m in matches)
        matches = [m for m in matches if m.session == last]
        orphans = [o for o in orphans if o[1] == last]
    if a.since:
        matches = [m for m in matches if m.start >= a.since]
        orphans = [o for o in orphans if o[0] >= a.since]

    out = ["SCOPS ranked match report", f"Log: {', '.join(str(p) for p in paths)}", ""]
    shown = hidden = 0
    sess = None
    for m in matches:
        quiet = (m.kind == "FREE" and not m.values and not m.recorded and not m.unfinished)
        if a.scored and quiet:
            hidden += 1
            continue
        if m.session != sess:
            sess = m.session
            st = starts[sess] if sess < len(starts) and starts[sess] else m.start
            out.append(f"===== Server run starting {st[:16]} =====")
        if quiet:
            out.append(f"Match {m.id}  {m.start[:16]}  room {m.room}  {m.map or 'map ?'}  FREE "
                       f"{MODE_NAMES.get(m.mode_start, m.mode_start)}  - no scores (normal for Free)")
        else:
            out.extend(render(m))
            out.append("")
        shown += 1

    # summary
    counted = [m for m in matches if m.recorded]
    flagged = [(m, f) for m in matches for f in flags_for(m)]
    out.append("")
    out.append("===== Summary =====")
    out.append(f"Matches: {len(matches)}   counted: {len(counted)}   "
               f"Ranking not counted: {sum(1 for m in matches if m.kind == 'RANKING' and m.end and not m.recorded)}   "
               f"Free: {sum(1 for m in matches if m.kind == 'FREE')}   flags: {len(flagged)}"
               + (f"   (hidden Free without scores: {hidden})" if hidden else ""))
    per = {}
    for m in counted:
        mode = m.mode_rec or "UNK"
        for n, d in applied(m).items():
            v = m.values.get(n, ["?"])
            side = "Spy" if v and v[0] == "0" else "Merc"
            p = per.setdefault(n, {})
            p[(mode, side)] = p.get((mode, side), 0) + d
            p[("games", "")] = p.get(("games", ""), 0) + 1
    if per:
        out.append("")
        out.append("Points this report covers (per mode and side):")
        for n in sorted(per, key=str.lower):
            p = per[n]
            parts = [f"{MODE_NAMES.get(mo, mo)} {sd} {v:+d}" for (mo, sd), v in sorted(p.items()) if mo != "games"]
            out.append(f"  {n:<16} {p[('games', '')]} match(es): " + ", ".join(parts))
    if flagged:
        out.append("")
        out.append("Needs a look:")
        for m, f in flagged:
            out.append(f"  match {m.id} ({m.start[:16]}, room {m.room}): {f}")
    if orphans:
        out.append("")
        out.append("Messages for matches the server did not know (usually after a restart mid-match):")
        for ts, _s, what in orphans:
            out.append(f"  {ts[:16]}  {what}")
    if not matches:
        out.append("No matches in this log" + (" for that time" if a.since or a.last else "") + ".")

    text = "\n".join(out) + "\n"
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
    dest = pathlib.Path(a.out) if a.out else paths[0].with_name("ranked_report.txt")
    try:
        dest.write_text(text, encoding="utf-8")
        print(f"(saved to {dest})")
    except Exception as e:
        print(f"(could not save {dest}: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
