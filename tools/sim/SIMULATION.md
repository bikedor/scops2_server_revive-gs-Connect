# Replay: warm holes and wake move (2026-11-13)

`warm_sim.py` replays the relay logs (`RELAY links` summaries, `HOLE steer`, `HOLE PING`) of real sessions and asks, for
every joiner socket that comes back after 60 s+ of silence (the reconnect after each map load): had the host sent through
that socket's relay hole recently enough? A reconnect counts as **at risk** if the hole was gone, or would be gone within
20 s (the host may still be loading the map), for a host that forgets a quiet hole after L seconds:
L = 120 s (PCSX2 Sockets mode closes a quiet link after 120 s) and L = 60 s (a strict home router).

Usage: `python warm_sim.py <udp_45000_reply.log> <host IP> <router_wm_40005.log>`

| Session (host) | Reconnects | L=120 s: now / warm / move / warm+move | L=60 s: now / warm / move / warm+move |
|---|---|---|---|
| Mount Hospital, Nov 12 (COOLDOR, PCSX2 Sockets, 2 players) | 7 | 2 / 1 / 0 / **0** | 6 / 5 / 0 / 2 |
| Nov 11 21:07 (DORIAN_D, 2 players) | 8 | 3 / 2 / 0 / **0** | 7 / 6 / 1 / 1 |
| Nov 11 night (DORIAN_D, 4 players, 2.5 h) | 78 | 72 / 38 / 38 / **17** | 72 / 60 / 45 / 51 |
| Nov 7 (JMIC) | 20 | 13 / 7 / 9 / **5** | 15 / 12 / 11 / 11 |
| Nov 8-9 (JMIC) | 110 | 92 / 62 / 48 / **26** | 97 / 89 / 60 / 67 |

- The absolute "now" numbers are what-ifs: DORIAN_D's fat PS2 on the modem clearly does not drop quiet holes (Nov 11 was
  clean), and JMIC's real failures were fewer. The reductions are the useful part.
- Warm holes alone is capped by the game: the host console pings one port, once every 25 s.
- Wake move lets a reconnect use a hole that is still open (usually the one that player's previous-round socket just left).
- Timing warm steers to the StartMatch/MatchFinish lobby messages was also tried: no gain, so it is not in the beta.
- New joiners found a freshly pinged spare hole in every session, with or without these changes.

## Relay scenarios: leave grace and no-reply move (2026-11-13)

`relay_scenarios.py` drives the real relay functions (`tools/nat2/udp_reply_server.py`) with a simulated clock, fake
sockets, a host console (pings its steered hole every 25 s, answers every packet it receives on the hole it came in on,
except while loading a map) and a network that can drop packets per hole and direction. Each scenario follows a sequence
from the 2026-11-13 logs and captures; "old" runs with `SCCT_RELAY_LEAVE_GRACE=0 SCCT_RELAY_NOREPLY_MOVE=0`.

Run from this folder: `python relay_scenarios.py` (add `-v` for the relay's own log lines). The server's `pylib` must be
on `PYTHONPATH` (the BETA launcher sets it; by hand: `set PYTHONPATH=..\..\pylib`).

| Scenario (logs of 2026-11-13) | Old | New |
|---|---|---|
| 05:29:16 leave while the link still sends (PS2 froze) | live link moved to another hole | keeps its hole |
| 05:28:25 leave + 05:28:47 rejoin (join never completed) | probe and join link on two holes | one hole, host answering |
| 04:30 host router drops relay packets from the ping-target hole | never answered | moved after 8 s, answered |
| 04:04 host's packets to one hole lost | never answered | moved after 8 s, answered |
| 04:05 steer whose ping never arrives, then a rejoin | rejoin split, onto the dead hole | steer marked failed after 60 s; rejoin on one working hole |
| Control: normal revenge reload (host silent 8 s, then answers) | no move | no move |
| Control: first join | one hole | one hole |

Limits: the model host answers at once and the network drops are all-or-nothing per hole; real joins also depend on game
logic the model does not have. "Join on one hole" is taken from the logs: every join that worked tonight had its first
contact and its game link on one hole, and the only one that split them failed.

## Relay scenarios: 4-player session (2026-11-13 22:29 - 2026-11-14 00:36)

Three scenarios added to `relay_scenarios.py`. The model host can now sit behind a filtering NAT (`World(nat=True)`):
relay packets get in only on holes its ping has opened, and the relay only sees (and can steer) pings from a session it
still knows.

| Scenario | Old | New |
|---|---|---|
| Host's SRP session on a rewritten port (4465): first joiner arrives, third joiner tries 5 times | session dropped as stray; third never answered | session kept; third in on try 1 |
| All three join within seconds (third retries every 13 s) | third never in | third in on try 4 (~42 s), no hole marked bad |
| Rejoin while another player's previous-map hole is idle but the host still sends there | put on that hole (join fails) | gets its own hole |

The second row is the limit of the fix: the console's ping opens one hole per ~25 s cycle, so a third player joining
right behind the second needs a retry or three. Joining one at a time, ~25 s apart, avoids it.

`relay_e2e_srp.py` checks the same fix with the relay's real SRP/NAT code (real SYN, NAT requests, pings and acks; only
sockets and clock replaced): with the host's session on port 4465 the old code drops it and opens no further hole, the
new code behaves exactly as for a host on 10070 (three joiners, three opened holes, logout handled). The 23:40:47
takeover (Fox given the hole Snake had just joined through) replays as in the log with the reuse guard off, and with it
on Snake keeps his hole and Fox gets the spare.

## Hold until pinged: hosting with no port forward (2026-11-14 22:08-22:13)

The model NAT can now also block a hole the way COOLDOR's router did with its forward off (`World(poison=True)`): a relay
packet that reaches a hole the host has not pinged leaves an entry that blocks the host's next ping there until 30 s after
the last such packet. With that, the old code reproduces the night exactly (second joiner never in while the first stays;
whoever is alone gets in). The earlier "fast third joiner" scenario passed in the model but failed for real because the
model let the ping through regardless.

| Scenario | Old | New |
|---|---|---|
| ANDROID-DOR joins, DORIAN_D 1 s later, retrying every 11 s | DORIAN_D never in (8 tries, 4 host pings blocked) | in on try 3, ANDROID-DOR stays connected |
| Both leave and rejoin over and over (10 joins) | 8 of 10 fail | all 10 in (worst: 3 tries) |
| Control: host that answers at once (forward / public IP), joiners 1 s apart | both in on try 1 | both in on try 1, judged open, nothing held |

Not proven: that early relay packets are what blocks the ping (it fits every hole of the night, and the same console
opened 45001 at login when nothing had been sent into it first). In the next real test the relay log tells: `holding ...`
followed by `hole N opened by the host after Ns` means it works; `holding ...` followed by `HOLE bad ... no ping in 60s`
means the router blocks second holes for another reason.

### After the first real test (2026-11-14 22:52-23:19)

Confirmed: every link the relay held on that host was let through by the host's own ping (after 4, 24, 23, 32, 39 s).
Two gaps fixed. The poison model now also expires holes: a hole the host has only pinged lasts 30 s after the ping (the
relay acks pings to the host's 10070, so that flow never sees a reply); one that carried traffic both ways lasts 180 s.

| Scenario | Old | New |
|---|---|---|
| 23:01:40 both players open a new link at once; the second is the probe that finds the host strict | ANDROID-DOR's link never answered (patch v1: held 59 s on the blocked hole) | answered after one ping cycle (15 s here) |
| Five map loads, both players reconnect on new sockets each time | 4 of 10 reconnects never answered | all 10 answered at once |

### Third fix (2026-11-15 00:34-00:58, DORIAN_D hosting SNAKE)

DORIAN_D's PS2 reports its public address at login (no router), yet at 00:41:23 it did not answer SNAKE's reconnect after
a map within 4 s while still sending to his other socket, and the relay judged it strict; StandaloneF0X's join at 00:54:55
was then held and failed.

| Scenario | v2 | v3 |
|---|---|---|
| Open host slow on one player's reconnect after a map; a new player joins later | (host judged strict, late joiner held) | not judged strict; late joiner in on try 1 |
| Host reports its public address at login, slow on its first join probe | - | open at once, nothing held |

### Spare pool

**v1 (2026-11-15, after the SNAKE / StandaloneF0X / Wickedness session, simulation only):** grew a trust window for a
kept spare from evidence (a re-ping that arrived, or a real link answered late), 3 spares kept at once. Simulated
results at the time: 6 match starts 150 s apart went from "16 at once, 1 within 30 s, 1 after 50 s" to "18 at once";
2 match starts after 10-minute matches went from "4/1/1" to "6 at once"; the harsh router-ignores-keepalives model held
even (pool switched itself off after two missed re-pings, about 2.5 min) and never did worse than no pool.

**Real test, 2026-11-15, COOLDOR hosting, no forward, UPnP off (4-player room, 10 logs, last map Vertigo Plaza):** v1's
core assumption broke. A link held on a kept spare was answered fine after 65 s and 71 s of silence, then another
failed at just 39 s and another at 42 s - success and failure interleaved at almost the same age, so there was no safe
age to trust. The pool's own 2-miss safety check did eventually catch it and switch itself off for COOLDOR, but not
before 2 real joins had already failed, and from Vertigo Plaza onward the room was back to plain hold-until-pinged
(ANDROID-DOR waited ~60 s and started the map as a spectator). Separately, re-running the harsh `out_only` router model
with 3 spares kept active at once (not just the trust window) showed the pool's own hole-opening/cycling traffic could
exhaust the relay's 12-hole budget via the pre-existing no-reply-move/bad-hole mechanism, making that scenario *worse*
than no pool at all (2 reconnects never answered, vs 0 for both no-pool and the old v1 design). Direct comparison of
`POOL_SIZE` (3, 2, 1, 0) found 1 spare matches the no-pool baseline exactly in that worst case; 2 or more does not.

**v2, tightened (current):** fixed trust ceiling `POOL_TRUST_S=35s`, not grown from evidence (growth was the thing
that broke); a single miss switches the pool off for that host for the session (not two); only **1** spare kept per
host, never more; the re-ping-of-a-kept-spare mechanism is removed entirely. Same room/router models as v1:

| Scenario | no pool | pool on (1 spare, fixed ceiling) |
|---|---|---|
| 6 match starts, 150 s apart (18 new links) | 16 at once, 1 within 30 s, 1 after 50 s | the same - a single spare helps at most one of three simultaneous joiners, not measurably different here |
| 2 match starts after 10-minute matches | 4 at once, 1 within 30 s, 1 after 50 s | the same, for the same reason |
| Router that ignores keepalives, 4 match starts | 7 at once, 4 within 30 s, 1 after 50 s | the same; the pool never places a real link on an unreliable spare in this model, so it never needs to switch itself off, and never does worse than no pool |
| Control: open host (forward / public IP) | all at once | all at once, no pool activity at all |

Honest reading: v2 is the safe version, not the effective one. It no longer risks making a strict host's room worse
than having no pool (the 2026-11-15 real failure and the 3-spare hole-exhaustion risk are both closed), but in every
simulated scenario above it is now indistinguishable from the no-pool baseline. In particular it would **not** be
expected to prevent the specific real-world symptom that motivated it (three joiners reconnecting together at a map
change, one left waiting) - it can only ever smooth over one joiner's reconnect at a time. Still not proven on a real
router; the real test above is the only field data point, and it tested v1, not v2.

### Rescue no-wait (2026-11-15, Cinema revenge swap, COOLDOR host)

`sc_revenge_room_link_rescue`: three joiners in a room on a host with no forward (router model: a hole lives 30 s after
the host's own last packet; relay packets into a closed hole block the host's next ping there). The room links go quiet
for a 130 s match, then come back at the revenge swap; the game leaves the room if its room link gets nothing for 58 s
(LINEBACKER: 04:59:52 -> 05:00:50).

| | old (waits up to 60 s for a pending rescue steer) | new (rescue no-wait) |
|---|---|---|
| Match start, LINEBACKER's game link on a never-opened hole | answered only after 53 s (moved once the steer timed out) | moved after 8 s to a hole the host had just answered on |
| Revenge swap, room links back | ANDROID-DOR 0 s, DORIAN_D 0 s, LINEBACKER never: booted to the lobby | all three 0 s |

All other scenarios give the same results as before (one, `sc_strict_held_link_follows_ping`, answers its second link
after 8 s instead of 13 s).

### Rebinding follow (2026-11-15, back to the lobby, COOLDOR host)

`sc_back_to_lobby_new_port`: three joiners; at back to the lobby each opens a new link, sends 9 s (~45 packets), the
host loads for 8 s, the room links stop, and after a ~7.7 s pause two joiners carry on from the same socket while
LINEBACKER carries on from a new port (as at 04:23:48 and 05:20:26).

| | old | new (rebinding follow) |
|---|---|---|
| LINEBACKER's new port | put on his old room link's hole (the host sees another address) | keeps the paused link's hole |

All other scenarios give the same results as before with the rule on.

### Owner keep (2026-11-15 05:38, Cinema revenge swap, COOLDOR host)

`sc_paused_link_keeps_hole`: three joiners; LINEBACKER's room link is quiet through the match and comes back at the
swap on a hole that has died; DORIAN_D's room link pauses 8 s for the load while the no-reply move looks for a hole.

| | old (5 s = gone) | new (owner keep) |
|---|---|---|
| Where LINEBACKER's room link goes | DORIAN_D's hole (DORIAN_D pushed off to another hole) | a free hole; DORIAN_D keeps his |

All other scenarios give the same results as before with the rule on (the room-link part is limited to a player's
oldest link: protecting every finished game link for 300 s cost a reconnect in the keepalive stress scenario).

### Strict host judged open after a load (2026-11-15, COOLDOR, forward and UPnP off)

`sc_strict_host_not_open_after_load`: strict host (poison model); at match start two new game links go into
never-pinged holes 1 s before the host's 7 s load; when the load ends the host's game sends to both first.

| | old | new (resume ignore) |
|---|---|---|
| Verdict | 'open' (holding off for the session) | still 'unknown' (tested again on the next cold-hole link) |

### Open verdict downgraded (2026-11-15 06:19, Mount Hospital revenge swap, COOLDOR host)

`sc_open_verdict_downgrade`: strict host (poison model, 1 spare) judged open, as after the 0.1 s answer at 06:00:02; a
match-start game link goes unanswered on a fresh hole; the room links are quiet through a 200 s match and come back at
the revenge swap.

| | old (stays open) | new (downgrade) |
|---|---|---|
| Room links after the swap | ANDROID-DOR 0 s, DORIAN_D 46 s (dropped in the real session) | ANDROID-DOR 0 s (ready spare), DORIAN_D 1 s (held until the host's ping) |

### Cold age on a strict host (2026-11-15 06:40, Factory revenge swap, COOLDOR host)

`sc_strict_used_hole_dies_early`: strict host whose router drops a used hole 50 s after the host's last packet and
ignores the relay's keepalives (`used_life=50, out_only`); three joiners; room links quiet through an 85 s match and
back at the swap. The warm rotation is slowed to stand in for the real session, where pool steers interleaved with it.

| | old (used hole trusted 170 s) | new (`HOLD_COLD_STRICT_S=60`) |
|---|---|---|
| Room links after the swap | ANDROID-DOR 17 s (sent into the dead hole; 65 s in the real session) | ANDROID-DOR 0 s (held, then a ready spare) |

`sc_rescue_to_fresh_pinged_hole`: the same with the cold-age fix off and every proven hole busy, a check that the
rescue path is no worse than before (61 s either way: with no freshly pinged spare free at that moment the move has
nothing to take; the cold-age rule is the fix for this case).

### AGE log (log only, 2026-11-15)

`sc_age_log`: the Factory reproduction above, with the AGE log on and off. Each room link back after the swap writes one
`AGE` line: the host's silence on its hole (game data, and any packet), the cold verdict, each hold / move / ready spare,
and the wait for the host's first packet. ANDROID-DOR's line: `kind=resume quiet=86s hole=45001 host_data=88s
host_any=88s cold=yes ctx=in-game outcome=answered wait=0.0s via=45006 steps=[0s cold 45001 -> ready spare 45006]`.

No decision changes: every scenario gives the same result with `SCCT_RELAY_AGE_LOG=0` and `=1`, and in all 58 scenario
runs the 4,658 non-AGE relay log lines (every hold, move, steer and keepalive) are identical with the log on and off.
About 8% extra lines (203 AGE lines over the suite, three quarters of them links answered at once).

Why it is needed: the Nov 13-15 relay logs cannot say how long a strict host's router keeps a used hole. There is no
first-reply time per link, and returning links are only logged when the silence log flagged them (the Factory room link
on hole 45007 at 06:40:07 is not in the log at all). Rebuilt from what was logged, COOLDOR answered used holes after
15-34 s and 37-48 s of silence but failed twice at 36 s; no link fell in the 50-59 s band, so moving the cold age from
60 to 50 s would have changed nothing that happened. Held links on COOLDOR waited 32-60 s for his ping (0 s with a ready
spare). `tools/sim/age_report.py <relay log>` turns AGE lines into that table per host.

### No-reply verdict on an untested host (2026-11-15 16:08, Cinema revenge swap, COOLDOR host)

`sc_untested_host_noreply_verdict`: a host left untested (its first test set aside, no later test), a router that drops
used holes after 50 s and ignores the relay's keepalives, three joiners, room links back at two swaps.

| | old (only an open verdict is revised) | new (`HOLD_NOREPLY_STRICT`) |
|---|---|---|
| Host state after swap 1 | untested | strict (first unanswered dead hole, at the match-1 start) |
| Room links at swap 1 | all three never answered (every hole dead at once: no proof for the no-reply move) | 0 s, 9 s, 8 s |
| Room links at swap 2 | all three never answered | 0 s, 9 s, 8 s |

In the scenario the first dead hole comes at the match-1 start; in the session it was the 16:08 swap itself, so the gain
there starts at the next swap. Safety check: with the "dead when the link started" condition removed, `sc_inbound_block`
(one blocked hole on a host that otherwise lets players in) is judged strict and gets 199 instead of 285 replies; with it,
no other scenario changes.

### Own-socket keep (2026-11-15 17:25-17:30, Krauser, COOLDOR host)

`sc_own_socket_keeps_hole`: DORIAN_D's PS2 with three fixed sockets taking turns; socket 1 has come back after a long
silence once, carries match 4 and pauses at the map change; 20 s later socket 2 comes back on its own dead hole while
the freshly pinged spare has just been taken by another link. The simulated host answers any address, so the check is
the hand-off itself (in the session the host then dropped the shared address and the PS2 left).

| | old | new (`OWN_SOCKET_KEEP`) |
|---|---|---|
| Socket 2 at the match start | given socket 1's hole 45004 | another spare (45002), answered at once |
| Socket 1 back at the revenge swap | answered after 6 s, on another hole | answered after 6 s |

A first version protected every socket of the player; it made links that used to land on a ready spare wait 15-16 s for
a ping in four scenarios (their game links use fresh ports and never come back). Limited to sockets that have come back
after 60 s+ of silence, no earlier scenario changes.

`sc_own_socket_long_halves` (real match length): two 10-minute halves, revenge swap, return to the lobby; DORIAN_D's
three sockets rotate, ANDROID-DOR (fresh ports) and a third player come back together each time; every returning link
finds a hole quiet for the whole half (dead on this router).

| | old | first version | new |
|---|---|---|---|
| Hand-offs of a returning socket's hole to his other socket | 3 | 1 (at the half-2 start: the socket had lost its own idle hole, so it counted as a new port) | 0 |
| Links answered at the two swaps / worst wait / never | 9 / 9 s / 0 | - | 9 / 16 s / 0 |

The 16 s is DORIAN_D's socket waiting for the host's ping instead of taking his other socket's hole. Window check on the
real code: with 1800 s a socket quiet 1200 s (two halves) is kept, 1801 s is not; with the old 900 s, 1200 s was not.

### Pool step-down (2026-11-15 23:59, FOX host)

`sc_pool_stepdown_fox_miss` (ported from Grok's draft): a four-player room on a strict host; one kept spare whose NAT
entry is already gone, silent 29 s when JMIC's link lands on it (34 s at the end of the 5 s trial, as logged). Then a
second link lands on a spare silent 14 s.

| | old | new (`POOL_STEPDOWN`) |
|---|---|---|
| First miss (34 s) | POOL OFF for the session | trust 35 s -> 20 s, pool stays on |
| Link on a 14 s spare after the step | - | not put on trial (a freshly pinged hole), pool stays on |

Grok's draft put 12-20 s spares on trial after the step so that a second miss could switch the pool off. Not kept: a
hole that young is the same hole a joiner would wait for with the pool off, so switching off there gains nothing.

Four-player map changes (`_pool_room`, 150 s / 330 s / 610 s matches, normal and keepalive-ignoring routers), pool at
35 s, at 20 s and off: 20 s and 35 s give identical waits in all six; in the 330 s keepalive-ignoring run both beat no
pool (median 22 s vs 37 s, worst 65 s vs 70 s). The simulation does not model the real benefit well (no pool and pool
tie in the normal runs), so the real check is the next session with a strict host.

### Shadow hole classes, stage 1 (2026-11-16, COOLDOR with UPnP)

New router model: `World(..., upnp=120)` (poison model) - a hole that never had an entry, or whose entry expired more than
120 s ago, lets relay packets in through the port forward; one that expired more recently drops them (COOLDOR's hole 45001).

`sc_shadow_upnp_router`: three joins and two swaps on that router. Shadow on: fresh tests 2 OK, stale tests 2 no-reply, no
verdict contradicts what the router did; at the first swap the host was judged open (today's routing already sends at
once), at the second it was strict and the relay held DORIAN_D 8.8 s and Wickedness 8.4 s on holes the router would have let
in - the two `WOULD send it through its own fresh hole` lines. Shadow off: no SHADOW lines.

`sc_upnp_hint_not_relayed`: a scopsDNS hint from the host's IP during a session - old: relayed to the joiner (1 datagram);
new: logged, not relayed.

Equivalence (every scenario, old and new, shadow on vs off, `_ids` reset per run): 68 runs, routing results and all 6,214
non-SHADOW log lines identical. Shadow verdict check (each `SHADOW test` against the packets the simulated router delivered
in that window): 145 tests, 0 wrong apart from `sc_outbound_block` (host replies blocked on the hole: no reply is the right
call from the relay). WOULD lines: only in `sc_shadow_upnp_router`; none for strict routers without a forward.

### UPnP router probe (2026-11-16, SCOPS window V7)

`sc_shadow_probe_proof`: the UPnP-like router of `sc_shadow_upnp_router`, with passive proof made impossible
(`SHADOW_PROVEN = 99`, as for COOLDOR whose fresh tests all fell in map-load bursts). new: a router-probe result (2/2 fresh
seen) is recorded at 400 s; the two held returns at the 469 s swap get `WOULD ... (router probe ...)` lines, none before
400 s. old: no probe, no WOULD lines.

The probe exchange itself is not simulated; it was run end to end against the real server on localhost with an emulated
window (`upnp_probe` request from the test port, `upnp_probe_seen` reports, an ack to the fresh port): all arrive, stale
dropped, nothing arrives, three addresses at once, and the busy / recent status replies.

### Failure steps (2026-11-16, log only)

`sc_fail_steps_log_only`: the `sc_untested_host_noreply_verdict` session (no-reply moves, strict verdict, bad holes) run
twice, with `SCCT_RELAY_FAIL_LOG` on and off. Routing lines and every delivery are identical; with it on there are
`FAIL why` lines on the moves and AGE lines with `avoided=` holes, with it off none. A stand-in NAT session with socket 3
stuck at PORT_ID gives one `NAT step ... stopped at 'PORT_ID sent'` line (not repeated) and one on the session's close.
Whole suite with `SCCT_RELAY_FAIL_LOG=0`: output identical to the run with it on (37/37 new OK). The "router dropped a hole
the host had just used" wording (COOLDOR 45002, 18:12) and the address-restricted evidence were checked by direct calls.

### Stricter open verdict (2026-11-17, switch off by default)

`sc_open_verdict_strict` calls the hold judge directly with the 23:17:04 order of events: the host's burst after a map load
begins on 45001 at 100.000, the player's first packet into cold hole 45006 at 100.010, the host's answer there at 100.050.
Switch off: "open" (today's false mark, reproduced). Switch on: still "unknown". A genuinely open host (clean answers 0.1 s
after the first packet, host sending steadily): off - open after one answer; on - unknown after the first, open after the
second. A strict host (no answer while sending on another hole): strict both ways. The older guard (burst after the first
packet) still holds both ways.

Whole suite, default (off): output identical to the run before this change. With `SCCT_RELAY_OPEN_STRICT=1`: 38/38 new OK;
three outputs differ - `sc_open_host_not_held` (host 'unknown' instead of 'open', nothing held either way),
`sc_strict_host_not_open_after_load` old mode (the Nov 15 false open no longer happens), `sc_shadow_upnp_router` (more
shadow would-lines, log only).

### Open verdict: 2-second clean window (2026-11-17)

`sc_open_verdict_strict`, added cases: E - the 02:37:40 pair (players sending since 100.0 s, probe timer restarted at
115.7/115.9, answers 116.0/116.2): too late, still unknown. F - an answer 0.4 s after the real first packet still counts
(clean answer 1 of 2). Through `relay_from_joiner` (the real send path, probe resets included): the first packet stays at
1000.0 and the answer 16.2 s later is too late. G - the same with the player quiet for 8.5 s mid-wait: still 1000.0 with the
30 s run gap; with the 5 s gap of the first draft it became 1014.6 and the late answer counted. Switch off: every case
behaves as before (both false opens reproduced). Whole suite on and off: identical to the previous run except this
scenario's own lines.

### Fair-handout shadow (2026-11-19, log only)
`sc_fair_shadow_log_only`: replays STRIDERSTROKE's 00:36:02 return. The host pings 45001, kept warm for DORIAN_D's idle
lobby socket; SNAKE's lobby socket arrives first and takes it (as live), DORIAN_D's arrives 0.1 s later and gets a free
hole that needs a steer. With `SCCT_RELAY_FAIR_SHADOW` on, the chosen holes are the same as with it off (45001, 45002),
and the lines appear: hole taken, the owner back "likely a slow return", and the summary. The failure-steps session also
routes identically on and off.

### Relay guard (2026-11-18, log only)
`sc_guard_log_only`: the failure-steps session (three joins, two map loads with bursts, a no-reply move) with
`SCCT_RELAY_GUARD_LOG` off, on (300 pkt/s), and on with an absurd 3 pkt/s limit. Routing lines and deliveries must be
identical in all three; with 3 pkt/s the `GUARD would-limit` lines appear (66), with 300 none; packets from an IP the lobby
never mapped are counted in `GUARD unmapped senders` and still dropped. The simulated links peak at 5 pkt/s: real numbers
come from the live log's `GUARD peaks` lines.

### Host lost its NAT session (2026-11-17, log only)

`sc_host_nat_lost_log`: a hosting console (COOLDOR, one player mapped, game data 1 s ago) closes its NAT session -> one
`NAT step: HOST ... closed its NAT session ... while hosting 1 player(s) (DORIAN_D)` line; it then pings without a session
-> one "pings without a NAT session (it closed it 9s ago)" line, not repeated. The same close 200 s after its last game data
(room over, normal logout) -> no line. With FAIL_LOG off: no lines. Whole suite with FAIL_LOG off: identical except this
scenario.

## Room check: Game Type and mode (2026-11-20)

`python tools/sim/room_kind_check.py .` drives the lobby server with fake messages: create labels, room update 31, START_MATCH 17,
SUBMIT_MATCH 30, MATCH_FINISH 45. Rooms modelled on the logs and the 2026-11-20 captures: SNAKE Free (`0x13`, no scores), IY0
Ranking (`0x0f`, scores; then a match without scores), STRIDERSTROKE created Ranking and switched to Free (`0x0f` -> `0x0b`,
scores still sent), and Ranking Sabotage switched to Free Sabotage (`0x0e` -> `0x0a`). Checks: labels from bit 0x04 and the low
2 bits, the switch lines, the "scores were sent although the room was Free" flag, `SCCT_RANKED_REQUIRE_RANKED=1` skipping the
Free match, `SCCT_RANKED_MODE_BITS=1` recording Free Sabotage as SAB (UNK with it off), and unchanged replies (38/39). Result: PASS.

## Fast resume flag (2026-11-20)

`sc_fast_resume_log_only` replays 2026-11-19 23:46:20 (SNAKE strict host): IY0's link, quiet 689 s, is answered in 0.0 s at the
round end and IY0's console leaves room 107 8.8 s later. With `SCCT_RELAY_FAST_RESUME_LOG` on: one `FAST RESUME` line and a
`follow-up ... LEFT the room 8.8s` line; DORIAN_D's normal return (answered after 15.4 s) is not flagged; a fast answer the
player stays through gets `follow-up ... stayed`. With it off: no FAST lines. The whole no-reply/strict session routes and
delivers identically on and off. Result: pass (whole suite passes). The same rule run over the real relay log from
2026-11-16 to 2026-11-20 finds exactly the two known cases (SNAKE 00:36:02, IY0 23:46:20) out of 129 round-end returns.

## UPnP-open shadow (2026-11-21)

`sc_upnp_open_shadow_log_only` replays 2026-11-21 01:19:16: COOLDOR (PCSX2 Sockets, UPnP forward reported 774 s earlier, router
probe fresh 4/4) is judged strict when ANDROID-DOR's link gets no reply for 18 s on hole 45002. With `SCCT_RELAY_UPNP_OPEN_SHADOW`
on: one `UPNP_OPEN shadow ... WOULD keep it open` line; a second host with no UPnP report gets `strict stands`. With it off: no
lines. Both verdicts are strict either way, and the whole no-reply/strict session routes and delivers identically on and off.
Result: pass (whole suite passes).

## Pause shadow (2026-11-21)

`sc_pause_shadow_log_only` replays 2026-11-21 03:20:29: COOLDOR judged strict on cold hole 45004; his last data on holes
45001-45003 was 2 s before and he sent nothing anywhere until 5 s after. With `SCCT_RELAY_PAUSE_SHADOW` on: one `PAUSE shadow
... WOULD cancel` line; a host sending every 0.2 s on another hole gets `verdict stands`. With it off: no lines. The whole
no-reply/strict session routes and delivers identically on and off. Result: pass (whole suite passes).

## Pause-aware strict verdict (2026-11-21)

`sc_pause_aware_verdict` drives the real cold-hole detector: (A) COOLDOR 04:16:27 (4.1 s unanswered, last data elsewhere 1.8 s
before, then silent) -> cancelled with `SCCT_RELAY_PAUSE_AWARE` on, strict with it off; (B) a strict host sending to its other
players every 0.2 s -> strict either way, 0.1 s later with it on; (C) third paused test after two cancels -> strict at once;
(D) whole no-reply/strict session: same deliveries and verdicts on and off. The whole suite was also run with the switch forced
on: 45/45 new-behaviour scenarios pass, results identical. Real-log replay of the 10 cold-hole verdicts since 2026-11-16 using the
relay's own host-silence lines: 6 stand, 4 would be cancelled (all COOLDOR 24.69.x, each in a 7.0-7.3 s host-wide silence).

`sc_pause_group_count` (2026-11-21 05:21, first live cancel): two joiners' links hit the same ~5 s COOLDOR pause 0.1 s
apart. Now they count as 1 cancel (the second logs "same pause as the previous cancel"); a later separate pause is cancel 2; a
third gives strict at once; a real strict host that keeps sending is still strict 0.1 s later. Full suite 46/46, also with
`SCCT_RELAY_PAUSE_AWARE` forced on.

## Loop timing (`sc_loop_stats_log_only`)
`SCCT_RELAY_LOOP_STATS` is off in code and log only. The scenario checks the latency buckets (p99 of 100 packets with one 6 ms and one 30 ms outlier is <10 ms), housekeeping and pause sums, the packets-waiting counts, one line per window with traffic, none for an idle window, and the reset. A live run of the real loop on localhost (beta settings from `run_public_test.bat`, relay ports 3658/10070-10080, ~1,400 game packets, 5 forced 3-packet NAT replies) gave game packets avg 0.03 ms in the server, max 0.2 ms, and 10 pauses of 25 ms, each with game packets queued behind it.
