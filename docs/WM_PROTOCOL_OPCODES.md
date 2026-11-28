# WM protocol opcode table (reference)

Extracted from `SC3_ON.ELF` (the game's own online module), a Ghidra export supplied 2026-11-27.

**What this is.** One function in that binary (`FUN_003a0dd0`, at image address `0x3a0dd0`) is a
pure opcode-number -> name lookup: given the single-byte WM command code, it returns/logs the
command's name, so this table is an authentic list of the WM message types this build of the
game recognizes.

**Cross-check against what we already knew from captures:** `UPDATEPLAYERPING` = 166 and
`UPDATEGROUPPING` = 202 here match exactly what we'd already independently worked out from
Wireshark captures (see the splinter-cell-gs-server notes: "never sent by the PS2 game, no ping
display in the menus"). That's a strong signal this table is accurate for our actual game build,
not a mismatched version from some other release.

**Important — this is a separate namespace from `subtype` in `ubigs_router_wm_server.py`.** The
`CREATE_ROOM` (12) / `GROUP_INFO` (53) / etc. constants our server already uses live *inside* a
`LOBBY_MSG` payload (WM opcode 209 below) — they are that sub-protocol's own, independently
numbered enum, nested one level down. This table is the **outer** WM opcode layer (the one
`LOBBY_MSG` itself is a single entry in, alongside session/arena/friends/clan/admin commands).
There is no numbering conflict between the two tables — they simply don't overlap, and this
export did not contain string names for the inner lobby-subtype enum (searching for `CREATE_ROOM`
/ `GROUP_INFO` with underscores in the decompiled source turned up nothing, so that inner table
isn't confirmable from this file).

**Which export to use.** The first export (generic MIPS profile) mis-decoded the EE's SQ/LQ/MMI
instructions, and 8,214 of its 9,011 functions ended in "bad instruction". A second export made
with the R5900 (PS2 EE) profile, supplied 2026-11-28, decodes properly: 9,966 functions, one bad
instruction, the PS2 kernel calls named, and the same addresses as the first export (same binary).
Use the R5900 export for any further work. The table below came out identical in both.

The game also has no names for the inner lobby subtypes: the function that prints a lobby subtype
(`0x3a0da0`) just prints the number. `CREATE_ROOM`, `GROUP_INFO` and the rest are our own names.
See `docs/ONLINE_FIXES.md` ("Client-side findings") for the lobby dispatcher and room parser.

## The table

Opcode byte (0-255, unsigned) -> WM command name. 14 of the names are short strings the code
reaches through the global pointer. Grok found `gp` = `0x004D9DF0` (set at `0x001001C0`: `lui a0,0x4e`,
`addiu a0,a0,-0x6210`, `move gp,a0`; 0x4E0000 - 0x6210 = 0x4D9DF0) and read the strings at `gp-0x2100`
to `gp-0x2098`, which gave GSFAIL, NEWS, CHAT, CHATALL, JOINNEW, PING, PAGER, LOGIN, PHOTO, SQLSET,
SQLSTAT, PEERMSG, SLEEP and WAKEUP.

**Checked against `ubi-gs-new/gsm.py`:** all 128 codes and names match our `MESSAGE_TYPE` enum exactly.
The enum has four codes the game's name table does not: 6 `EVENT_UDPCONNECT`, 204 `PROXY_HANDLER`,
219 `KEY_EXCHANGE` and 221 `NAT`. The game does use 204: its lobby code treats 204 like `LOBBY_MSG`
(209) when it reads replies. It just has no printable name for it.

| Code | Command |
|---|---|
| 1 | NEWUSERREQUEST |
| 2 | CONNECTIONREQUEST |
| 3 | PLAYERNEW |
| 4 | DISCONNECTION |
| 5 | PLAYERREMOVED |
| 7 | NEWS |
| 8 | SEARCHPLAYER |
| 9 | REMOVEACCOUNT |
| 11 | SERVERSLIST |
| 13 | SESSIONLIST |
| 15 | PLAYERLIST |
| 16 | GETGROUPINFO |
| 17 | GROUPINFO |
| 18 | GETPLAYERINFO |
| 19 | PLAYERINFO |
| 20 | CHATALL |
| 21 | CHATLIST |
| 22 | CHATSESSION |
| 24 | CHAT |
| 26 | CREATESESSION |
| 27 | SESSIONNEW |
| 28 | JOINSESSION |
| 31 | JOINNEW |
| 32 | LEAVESESSION |
| 33 | JOINLEAVE |
| 34 | SESSIONREMOVE |
| 38 | GSSUCCESS |
| 39 | GSFAIL |
| 40 | BEGINGAME |
| 45 | UPDATEPLAYERINFO |
| 48 | MASTERCHANGED |
| 51 | UPDATESESSIONSTATE |
| 52 | URGENTMESSAGE |
| 54 | NEWWAITMODULE |
| 55 | KILLMODULE |
| 58 | STILLALIVE |
| 59 | PING |
| 60 | PLAYERKICK |
| 61 | PLAYERMUTE |
| 62 | ALLOWGAME |
| 63 | FORBIDGAME |
| 64 | GAMELIST |
| 65 | UPDATEADVERTISMEMENTS |
| 66 | UPDATENEWS |
| 67 | VERSIONLIST |
| 68 | UPDATEVERSIONS |
| 70 | UPDATEDISTANTROUTERS |
| 71 | ADMINLOGIN |
| 72 | STAT_PLAYER |
| 73 | STAT_GAME |
| 74 | UPDATEFRIEND |
| 75 | ADDFRIEND |
| 76 | DELFRIEND |
| 77 | LOGINWAITMODULE |
| 78 | LOGINFRIENDS |
| 79 | ADDIGNOREFRIEND |
| 80 | DELIGNOREFRIEND |
| 81 | STATUSCHANGE |
| 82 | JOINARENA |
| 83 | LEAVEARENA |
| 84 | IGNORELIST |
| 85 | IGNOREFRIEND |
| 86 | GETARENA |
| 87 | GETSESSION |
| 88 | PAGEPLAYER |
| 89 | FRIENDLIST |
| 90 | PEERMSG |
| 91 | PEERPLAYER |
| 92 | DISCONNECTFRIENDS |
| 93 | JOINWAITMODULE |
| 94 | LOGINSESSION |
| 95 | DISCONNECTSESSION |
| 96 | PLAYERDISCONNECT |
| 97 | ADVERTISEMENT |
| 98 | MODIFYUSER |
| 99 | STARTGAME |
| 100 | CHANGEVERSION |
| 101 | PAGER |
| 102 | LOGIN |
| 103 | PHOTO |
| 104 | LOGINARENA |
| 106 | SQLCREATE |
| 107 | SQLSELECT |
| 108 | SQLDELETE |
| 109 | SQLSET |
| 110 | SQLSTAT |
| 111 | SQLQUERY |
| 127 | ROUTEURLIST |
| 131 | DISTANCEVECTOR |
| 132 | WRAPPEDMESSAGE |
| 133 | CHANGEFRIEND |
| 134 | NEWRELFRIEND |
| 135 | DELRELFRIEND |
| 136 | NEWIGNOREFRIEND |
| 137 | DELETEIGNOREFRIEND |
| 138 | ARENACONNECTION |
| 139 | ARENADISCONNECTION |
| 140 | ARENAWAITMODULE |
| 141 | ARENANEW |
| 143 | NEWBASICGROUP |
| 144 | ARENAREMOVED |
| 145 | DELETEBASICGROUP |
| 146 | SESSIONSBEGIN |
| 148 | GROUPDATA |
| 151 | ARENA_MESSAGE |
| 157 | ARENALISTREQUEST |
| 158 | ROUTERPLAYERNEW |
| 159 | BASEGROUPREQUEST |
| 166 | UPDATEPLAYERPING |
| 169 | UPDATEGROUPSIZE |
| 179 | SLEEP |
| 180 | WAKEUP |
| 181 | SYSTEMPAGE |
| 189 | SESSIONOPEN |
| 190 | SESSIONCLOSE |
| 192 | LOGINCLANMANAGER |
| 193 | DISCONNECTCLANMANAGER |
| 194 | CLANMANAGERPAGE |
| 195 | UPDATECLANPLAYER |
| 196 | PLAYERCLANS |
| 199 | GETPERSISTANTGROUPINFO |
| 202 | UPDATEGROUPPING |
| 203 | DEFERREDGAMESTARTED |
| 205 | BEGINCLIENTHOSTGAME |
| 209 | LOBBY_MSG |
| 210 | LOBBYSERVERLOGIN |
| 211 | SETGROUPSZDATA |
| 212 | GROUPSZDATA |

## Lobby replies and pushes: what the client reads (R5900 export)

Every function that waits for a lobby message of one subtype (`0x376f10(conn, subtype)`) and then
reads its fields. Readers: `list` = sub-list, `int`, `str`, `strN` = string with a size limit,
`short`. The number in brackets is the field index (some are computed in the code and shown as
the decompiler wrote them). Replies come as `['38',[subtype,[payload]]]`, failures as
`['39',[subtype,[...]]]`, so most start with `list[1] list[1]` (into `[subtype,[payload]]`, then
into the payload). Spot checks against `ubigs_router_wm_server.py`: 12 (`[roomId, roomName,
lobbyId]`) and 9 (default `flat` reply, two ints) both match what the client reads.

| Subtype | Parser | Fields read, in order |
|---|---|---|
| 3 | 0x37c8b0 | list[1] list[1] int[1] int[0] str[1] int[2] |
| 8 | 0x37f360 | list[1] list[1] int[0 or 1] |
| 9 | 0x37b910 | list[1] list[1] int[0] int[1] |
| 10 | 0x37e380 | list[1] list[1] int str |
| 12 | 0x37d0b0 | list[1] list[1]; fail: str[1]; ok: int[0] str[1] int[2] |
| 14 | 0x37edb0 | list[1] list[1] int[1] int[0] int[1] |
| 15 | 0x380830 | list[1] list[1] int[0 or 1] |
| 17 | 0x37d6f0 | list[1] list[1] int[0 or 1] |
| 18 | 0x37b550 | (none) |
| 19 | 0x378440 | list[1] int[0] |
| 21 | 0x37b4f0 | (none) |
| 23 | 0x37bd80 | list[1] list[1] int str |
| 24 | 0x37c1e0 | list[1] list[1] int str |
| 27 | 0x37de40 | list[1] list[1] str int |
| 30 | 0x37ff10 | list[1] list[1] int[0 or 1] |
| 31 | 0x37fa70 | list[1] list[1] int[0 or 1] |
| 32 | 0x3803a0 | list[1] int[0] list str[0] short[1] |
| 33 | 0x380cd0 | list[1] list[1] int[0 or 1] |
| 36 | 0x381de0 | list[1] list[1] int str |
| 40 | 0x382320 | list[1] list[1] int str |
| 41 | 0x381940 | list[1] list[1] int[0 or 1] |
| 45 | 0x385700 | short[0] int[0] int[0] int[1] |
| 46 | 0x385a60 | short[0] int[0] int[0] int[1] |
| 50 | 0x384ae0 | short[0] list[1] str[0] int[2] str[3] str[4] short[6] |
| 51 | 0x385190 | list[1] strN[0x21] int |
| 53 | 0x383400 | short[0] list[1] ... str[0] str[2] str[3] short[6] short[7] (room/lobby list, nested) |
| 54 | 0x382fc0 | short[0] list[1] (then the 20-field room parser 0x37b670) |
| 55 | 0x37ce70 | list[1] int[0] |
| 56 | 0x377aa0 | list[1] |
| 57 | 0x37eb40 | list[1] int[0] int[1] |
| 59 | 0x37db90 | list[1] int[0] str[1] str[2] str[3] |
| 61 | 0x37e8c0 | list[1] int[0] strN[0x81] |
| 62 | 0x37f800 | list[1] int[0] |
| 63 | 0x382860 | list[1] int[0] strN[0x81] |
| 64 | 0x3866d0 | short[0] list[1] int[0] str[1] str |
| 65 | 0x382ae0 | list[1] int[0] |
| 66 | 0x386af0 | list[1] str[0] |
| 69 | 0x385860 | strN[0x10] short[1] |
| 71 | 0x387330 | list[1] list strN[0x21] list[1] int int[0] |
| 106 | 0x386f80 | list[1] str[0] list int[0] int[1] |
| 151 | 0x3860b0 | short[0] list[1] int[0] list[1] str[0] list |
| 210 | 0x37c640 | list[1] int[0] int[1] int[0] |
