#!/usr/bin/env python3
"""scopsDNS - local DNS server for Splinter Cell Chaos Theory PS2 online (SCOPS).

Based on insigniaDNS (Insignia) / sudomemoDNS (Team Sudomemo, MIT licence), rebuilt for the SCOPS server.

Use it when your internet provider, apartment network or router blocks or rewrites DNS to outside servers, so a
console set straight to the SCOPS DNS (38.49.212.120) cannot use it. Run it on a computer on the same network as the
PS2 / PCSX2 and set the console's DNS to the address it prints:

  * the names the game looks up (Ubisoft, GameSpy, DNAS) are answered here with the SCOPS server address,
    so these lookups never leave your network;
  * every other name is forwarded to a public resolver (unlike insigniaDNS, so one DNS entry is enough).

Optional: --open-firewall allows DNS (UDP/TCP 53) in for this program on Windows, macOS or Linux,
and --pcsx2-ports also opens the game's UDP ports (3658, 10070-10080) for PCSX2 running on this computer.

Only the Python standard library is used. Python 3.8 or newer. Not affiliated with Ubisoft, Sony or Insignia.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import os
import platform
import random
import re
import secrets
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "2.1"
DEFAULT_SERVER = "38.49.212.120"
# Same names the SCOPS server's own DNS answers. The game itself only looks up
# gsconnect.ubisoft.com and gate1.us.dnas.playstation.org (plus "noname").
GAME_SUFFIXES = ("ubisoft.com", "ubi.com", "gamespy.com", "gamespy.net", "dnas.playstation.org")
GAME_NAMES = ("noname",)
DEFAULT_UPSTREAMS = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
LOBBY_TCP_PORT = 40000  # only for the start-up reachability check
GAME_UDP_PORTS = "3658,10070-10080"
TTL = 60
RULE = "scopsDNS"

QTYPE_A = 1
QTYPE_NAMES = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV",
               65: "HTTPS", 255: "ANY"}

_print_lock = threading.Lock()


def say(level: str, msg: str) -> None:
    with _print_lock:
        print(f"[{datetime.datetime.now():%H:%M:%S}] [{level}] {msg}", flush=True)


def os_name() -> str:
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(platform.system(), platform.system())


def get_ip() -> str:
    """This computer's address on its main network: the card its internet traffic leaves by.
    Works out the route only; no packet is sent. Falls back to insigniaDNS's method."""
    for target in ("8.8.8.8", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith(("0.", "127.")):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return "127.0.0.1"


def all_ips() -> list:
    """Every IPv4 address of this computer (to show alternatives when it has several network cards)."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith(("127.", "169.254.")))


def format_ip(address: str) -> str:
    """192.168.1.5 -> 192.168.001.005, the way the PS2 network settings show it."""
    return ".".join(f"{int(o):03d}" for o in address.split("."))


# ----------------------------------------------------------------------------------------------- DNS
def parse_question(data: bytes):
    """(name, qtype, end of question) for a normal single-question query, else None."""
    if len(data) < 12:
        return None
    flags, qdcount = struct.unpack_from("!HH", data, 2)
    if flags & 0x8000 or qdcount != 1:
        return None
    labels, off = [], 12
    while True:
        if off >= len(data):
            return None
        n = data[off]
        off += 1
        if n == 0:
            break
        if n & 0xC0 or off + n > len(data):
            return None
        labels.append(data[off:off + n].decode("ascii", "replace"))
        off += n
    if off + 4 > len(data):
        return None
    qtype = struct.unpack_from("!H", data, off)[0]
    return ".".join(labels).lower(), qtype, off + 4


def is_game_name(name: str) -> bool:
    name = name.rstrip(".")
    return name in GAME_NAMES or any(name == s or name.endswith("." + s) for s in GAME_SUFFIXES)


def game_answer(query: bytes, q_end: int, qtype: int, ip: str) -> bytes:
    """A record with the SCOPS address for A/ANY; an empty answer for anything else (IPv4 only)."""
    rd = struct.unpack_from("!H", query, 2)[0] & 0x0100
    flags = 0x8000 | 0x0400 | rd | 0x0080
    ans = b""
    if qtype in (QTYPE_A, 255):
        ans = struct.pack("!HHHIH", 0xC00C, QTYPE_A, 1, TTL, 4) + socket.inet_aton(ip)
    return query[:2] + struct.pack("!HHHHH", flags, 1, 1 if ans else 0, 0, 0) + query[12:q_end] + ans


def servfail(query: bytes) -> bytes:
    q = parse_question(query)
    qsec = query[12:q[2]] if q else b""
    rd = struct.unpack_from("!H", query, 2)[0] & 0x0100
    return query[:2] + struct.pack("!HHHHH", 0x8000 | rd | 0x0080 | 2, 1 if qsec else 0, 0, 0, 0) + qsec


def forward(query: bytes, upstreams) -> bytes | None:
    for up in upstreams:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.5)
        try:
            s.sendto(query, (up, 53))
            while True:
                resp, src = s.recvfrom(4096)
                if src[0] == up and resp[:2] == query[:2]:
                    return resp
        except OSError:
            continue
        finally:
            s.close()
    return None


def _dns_query(name: str, qtype: int = QTYPE_A, qclass: int = 1) -> bytes:
    """Minimal DNS query (one question, recursion desired)."""
    tid = random.randint(0, 65535)
    flags = 0x0100  # RD
    labels = b"".join(struct.pack("!B", len(part)) + part.encode("ascii") for part in name.split(".")) + b"\x00"
    return struct.pack("!HHHHHH", tid, flags, 1, 0, 0, 0) + labels + struct.pack("!HH", qtype, qclass)


CANARY_IP = "192.0.2.1"   # reserved test address: no DNS server can be there


def _who(server: str) -> str:
    """Name of whatever answers DNS sent to server (version.bind / id.server), or ''.
    The real SCOPS DNS refuses these, so any name here belongs to someone else."""
    for name in ("version.bind", "id.server"):
        r = _probe(server, name, qtype=16, qclass=3, timeout=1.5)
        if r["ok"] and r["txt"]:
            return r["txt"][0][:60]
    return ""


def _src_kind(src: str, server: str, own_ips) -> str:
    """Who sent a DNS reply, as far as this PC can tell.
    pc     : this PC itself (127.x or one of its own addresses): VPN, antivirus, DNS filter
    router : another home-network address: the router answers DNS for every device
    server : claims to be the SCOPS server (real, or forged by whatever intercepts DNS)
    remote : some other internet address
    """
    if not src:
        return ""
    if src.startswith("127.") or src in own_ips:
        return "pc"
    if src == server:
        return "server"
    try:
        if ipaddress.IPv4Address(src).is_private:
            return "router"
    except ValueError:
        pass
    return "remote"


def _classify_dns(server: str, wrong: list, canary_ok: bool, fake: dict, who: str, own_ips=None) -> tuple:
    """Decide what a failed direct check means.

    Red ("hijack"): the answer came from another internet server, or from the
    router (the PS2 sits behind the same router, so it is affected too).
    Amber ("filter"): the answer came from this PC, or was forged in the SCOPS
    server's name, or even 192.0.2.1 answered. From the PC alone that can be a
    program on this PC or the provider, so: try the PS2 first, local DNS if it fails.
    """
    if own_ips is None:
        own_ips = set(all_ips()) | {get_ip()}
    by = f" (it calls itself '{who}')" if who else ""
    for w in wrong:
        w["kind"] = _src_kind(w.get("src", ""), server, own_ips)
    fake_ok = bool(fake.get("ok"))
    fake_rcode = fake.get("rcode")
    fake_src = fake.get("src") or ""
    substituted = fake_ok and fake_rcode != 5
    fake_kind = _src_kind(fake_src, server, own_ips) if substituted else ""
    server_refused = fake_ok and fake_rcode == 5 and fake_src == server

    remote = [w for w in wrong if w["kind"] == "remote"]
    router = [w for w in wrong if w["kind"] == "router"]
    if remote or router or fake_kind in ("remote", "router"):
        if router or fake_kind == "router":
            src = router[0]["src"] if router else fake_src
            reason = f"your router ({src}) answers DNS itself{by}"
        else:
            src = remote[0]["src"] if remote else fake_src
            reason = f"questions sent to {server} are answered by another DNS server ({src}){by}"
        detail = "; ".join(w["text"] for w in wrong) if wrong else f"a test name came back from {fake_src}"
        return "hijack", [
            f"ISP DNS hijack detected: {reason}, not {server}.",
            f"detail: {detail}",
            "A PS2 set straight to the SCOPS DNS will not work from this network. Use the local DNS.",
        ]

    pc = [w for w in wrong if w["kind"] == "pc"]
    forged = [w for w in wrong if w["kind"] == "server"]
    if canary_ok or pc or forged or fake_kind in ("pc", "server"):
        if pc:
            where = pc[0]["text"]
        elif forged:
            where = forged[0]["text"].replace(f"from {server}", f"in the name of {server}")
        elif canary_ok:
            where = "a question sent to 192.0.2.1, where no DNS server exists, was answered"
        else:
            where = f"a test name came back with code {fake_rcode}"
        extra = " The SCOPS server itself refused a test name, so it is reachable." if server_refused else ""
        return "filter", [
            f"PC DNS filter: DNS on this PC or your network is answered by something else ({where}){by}.{extra}",
            f"Put {server} in the PS2 first. If the game does not get online (DNAS error), use the local DNS.",
        ]

    if server_refused:
        return "retry", [
            f"Could not get a SCOPS answer from {server}, but the server itself is reachable "
            f"(it refused a test name as expected). Try again in a minute.",
        ]

    return "none", [
        f"Could not get a SCOPS answer from {server} (no reply).",
        "Use the local DNS. The SCOPS DNS may be blocked on this network even if public DNS works.",
    ]


def detect_dns_hijack(upstreams, server: str) -> None:
    """Green when the SCOPS DNS itself answers a game name with its own address.

    Otherwise classify the failure. A reply from another internet host is a
    hijack. A reply from this PC, or an answer to a query sent where no DNS
    server can exist, is only a filter on this PC: the PS2 may still work.
    """
    names = ["gsconnect.ubisoft.com", "gate1.us.dnas.playstation.org", "gsconnect.ubisoft.com"]
    wrong = []
    for name in names:
        r = _probe(server, name)
        if r["ok"] and server in r["answers"] and r["src"] == server:
            say("INFO", f"SCOPS DNS {server} answered {name} with {server}.")
            say("INFO", "no hijack detected: your ISP lets you use the SCOPS DNS directly.")
            return
        if r["ok"] and not (r["rcode"] == 5 and r["src"] == server):
            got = ", ".join(r["answers"]) if r["answers"] else f"no address (code {r['rcode']})"
            where = "this PC or your home network" if r["local_reply"] else r["src"]
            wrong.append({
                "text": f"{name} came back as {got} from {where}",
                "local": bool(r["local_reply"]),
                "src": r["src"],
            })
        time.sleep(0.3)

    canary = _probe(CANARY_IP, "gsconnect.ubisoft.com", timeout=1.5)
    made_up = f"scops-check-{secrets.token_hex(4)}.invalid"
    fake = _probe(server, made_up, timeout=1.5)
    interesting = canary["ok"] or wrong or (fake["ok"] and fake["rcode"] != 5)
    who = _who(server) if interesting else ""
    say("INFO", f"checks: 192.0.2.1 {'answered' if canary['ok'] else 'silent (good)'}; "
                f"made-up name "
                + ("answered, code " + str(fake["rcode"]) if fake["ok"] and fake["rcode"] != 5
                   else ("refused (good)" if fake["ok"] else "no reply"))
                + (f"; answering server calls itself '{who}'" if who else ""))

    kind, lines = _classify_dns(server, wrong, canary["ok"], fake, who)
    for line in lines:
        say("WARN", line)
    return


def _txt_records(resp: bytes) -> list:
    """TXT strings in a DNS response (first string of each record)."""
    found = []
    try:
        off = 12
        qd, an = struct.unpack_from("!HH", resp, 4)
        for _ in range(qd):
            while resp[off] != 0:
                if resp[off] & 0xC0:
                    off += 1
                    break
                off += 1 + resp[off]
            off += 5
        for _ in range(an):
            if resp[off] & 0xC0:
                off += 2
            else:
                while resp[off] != 0:
                    off += 1 + resp[off]
                off += 1
            rtype, _, _, rdlen = struct.unpack_from("!HHIH", resp, off)
            off += 10
            if rtype == 16 and rdlen > 0:
                ln = resp[off]
                found.append(resp[off + 1:off + 1 + ln].decode("ascii", "replace"))
            off += rdlen
    except (IndexError, struct.error):
        return found
    return found


def _a_records(resp: bytes) -> list:
    """IPv4 answers in a DNS response. Skips a broken packet instead of raising."""
    found = []
    try:
        off = 12
        qd = struct.unpack_from("!H", resp, 4)[0]
        an = struct.unpack_from("!H", resp, 6)[0]
        for _ in range(qd):
            while resp[off] != 0:
                if resp[off] & 0xC0:
                    off += 2
                    break
                off += 1 + resp[off]
            else:
                off += 1
            off += 4
        for _ in range(an):
            if resp[off] & 0xC0:
                off += 2
            else:
                while resp[off] != 0:
                    off += 1 + resp[off]
                off += 1
            rtype, _, _, rdlen = struct.unpack_from("!HHIH", resp, off)
            off += 10
            if rtype == 1 and rdlen == 4:
                found.append(socket.inet_ntoa(resp[off:off + 4]))
            off += rdlen
    except (IndexError, struct.error):
        return found
    return found


def _probe(server: str, name: str, qtype: int = QTYPE_A, qclass: int = 1, timeout: float = 2.5) -> dict:
    """One UDP DNS query. ok False means no usable reply."""
    query = _dns_query(name, qtype, qclass)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(query, (server, 53))
        resp, src = s.recvfrom(4096)
    except OSError:
        return {"ok": False, "src": "", "rcode": -1, "ancount": 0, "answers": [], "txt": [], "local_reply": False}
    finally:
        s.close()
    if len(resp) < 12 or resp[:2] != query[:2]:
        return {"ok": False, "src": src[0], "rcode": -1, "ancount": 0, "answers": [], "txt": [], "local_reply": False}
    flags = struct.unpack_from("!H", resp, 2)[0]
    ancount = struct.unpack_from("!H", resp, 6)[0]
    try:
        local = ipaddress.IPv4Address(src[0]).is_private or src[0].startswith("127.")
    except ValueError:
        local = False
    return {"ok": True, "src": src[0], "rcode": flags & 0x000F, "ancount": ancount,
            "answers": _a_records(resp), "txt": _txt_records(resp), "local_reply": local}


_SEEN_CONSOLES = set()

# ---- Find my PS2 -------------------------------------------------------------------------------------------------
# Sony's registered MAC prefixes (IEEE MA-L: 45 "Sony Interactive Entertainment Inc." + 00:22:A6 "Sony Computer
# Entertainment America"; checked 2026-11-21 against maclookup.app). PS2 era: 00:04:1F (registered 2000-11-09, PS2
# network adapter, also PCSX2's emulated adapter), 00:13:15 (2004-12-15, slim PS2: seen on a real console 2026-11-21)
# and 00:15:C1 (2005-09-09, later slim PS2s, before the PS3). The others are later Sony devices (PS3/PS4/PS5, PSP...),
# still listed so a PS2 on another prefix is not missed.
PS2_ERA_OUIS = {"00041f", "001315", "0015c1"}
SONY_OUIS = PS2_ERA_OUIS | {
    "001315", "0015c1", "0019c5", "001d0d", "001fa7", "00248d", "00d9d1", "00e421", "04f778", "0c7043", "0cfe45",
    "280dfc", "2840dd", "2c9e00", "2ccc44", "50b03b", "54e6fd", "5c843c", "5c9666", "68286c", "70662a", "709e29",
    "78c881", "78e04d", "84e657", "904748", "98fa2e", "9c37cb", "a8e3ee", "b40ad8", "b41f4d", "bc3329", "bc60a7",
    "c0151b", "c84aa0", "c863f1", "d4f7d5", "e86e3a", "ec748c", "f46412", "f8461c", "f8d0ac", "fc0fe6", "fcca40",
    "0022a6"}


def _norm_mac(m: str) -> str:
    """'0:4:1f:82:a:b' (macOS), '00-04-1f-82-0a-0b' (Windows), '00:04:1F:..' -> '00041f820a0b' ('' if not a MAC)."""
    parts = re.split(r"[:\-]", m.strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", x) for x in parts):
        return ""
    return "".join(x.zfill(2) for x in parts)


def neighbour_table() -> dict:
    """IPv4 address -> normalised MAC from the OS neighbour (ARP) table. No admin rights needed."""
    osn = os_name()
    out = ""
    cmds = ([["arp", "-a"]] if osn == "Windows" else
            [["arp", "-an"]] if osn == "macOS" else [["ip", "-4", "neigh"], ["arp", "-an"]])
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=10)
            if r.stdout.strip():
                out = r.stdout
                break
        except (OSError, subprocess.SubprocessError):
            continue
    if not out and osn == "Linux":
        try:
            out = open("/proc/net/arp").read()
        except OSError:
            pass
    table = {}
    for line in out.splitlines():
        ipm = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        macm = re.search(r"\b([0-9a-fA-F]{1,2}(?:[:\-][0-9a-fA-F]{1,2}){5})\b", line)
        if ipm and macm:
            mac = _norm_mac(macm.group(1))
            if mac and mac not in ("000000000000", "ffffffffffff"):
                table[ipm.group(1)] = mac
    return table


def _fmt_mac(m: str) -> str:
    return ":".join(m[i:i + 2] for i in range(0, 12, 2))


def find_ps2(my_ip: str, wait: float = 2.5) -> list:
    """Nudge every address on this computer's /24 (one tiny UDP datagram to the discard port, so the OS looks up each
    address on the network), then read the neighbour table and return the Sony devices, PS2-era first.
    The PS2 only answers while its network is up (the game's online menu)."""
    try:
        net = ipaddress.IPv4Network(f"{my_ip}/24", strict=False)
    except ValueError:
        return []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for host in net.hosts():
            h = str(host)
            if h == my_ip:
                continue
            try:
                s.sendto(b"scops", (h, 9))
            except OSError:
                pass
    finally:
        s.close()
    time.sleep(wait)
    found = []
    for ip, mac in neighbour_table().items():
        try:
            if ipaddress.IPv4Address(ip) not in net or ip == my_ip:
                continue
        except ValueError:
            continue
        if mac[:6] in SONY_OUIS:
            found.append((0 if mac[:6] in PS2_ERA_OUIS else 1, ip, mac))
    found.sort(key=lambda t: (t[0], tuple(int(x) for x in t[1].split("."))))
    return found


def report_find_ps2(my_ip: str) -> int:
    say("INFO", f"Looking for a PS2 on {my_ip.rsplit('.', 1)[0]}.x (takes a few seconds). "
                "The PS2 must be at the game's online menu so its network is on.")
    found = find_ps2(my_ip)
    if not found:
        say("WARN", "PS2 NOT FOUND: no Sony network device answered. Start the game's online menu on the PS2 "
                    "(past the network connection screen), check it is on the same network as this computer "
                    "(not a guest Wi-Fi), then try again.")
        return 1
    for era, ip, mac in found:
        kind = "PS2-era Sony network adapter (or PCSX2)" if era == 0 else "other Sony device (PS3/PS4/PS5?)"
        say("INFO", f"{'PS2 FOUND' if era == 0 else 'SONY DEVICE'}: {ip} (MAC {_fmt_mac(mac)}, {kind})")
    return 0


def answer(data: bytes, client_ip: str, cfg) -> bytes | None:
    q = parse_question(data)
    if q is None:
        say("ERROR", f"Invalid DNS request from {client_ip}")
        return None
    name, qtype, q_end = q
    if is_game_name(name):
        if client_ip not in _SEEN_CONSOLES:
            _SEEN_CONSOLES.add(client_ip)
            say("INFO", f"PS2 address seen: {client_ip} (it asked this computer for {name}; use it for hosting)")
        if qtype in (QTYPE_A, 255):
            say("INFO", f"Console at {client_ip} looked up {name} -> {cfg.server} (SCOPS)")
        elif cfg.verbose:
            say("INFO", f"Console at {client_ip} looked up {name} ({QTYPE_NAMES.get(qtype, qtype)}) -> no record")
        return game_answer(data, q_end, qtype, cfg.server)
    resp = forward(data, cfg.upstream)
    if cfg.verbose:
        say("INFO", f"{client_ip} looked up {name} ({QTYPE_NAMES.get(qtype, qtype)}) -> forwarded"
                    + ("" if resp else ", no reply"))
    return resp if resp is not None else servfail(data)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        reply = answer(data, self.client_address[0], self.server.cfg)
        if reply:
            sock.sendto(reply, self.client_address)


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            while True:
                hdr = self._read(2)
                if not hdr:
                    return
                data = self._read(struct.unpack("!H", hdr)[0])
                if not data:
                    return
                reply = answer(data, self.client_address[0], self.server.cfg)
                if not reply:
                    return
                self.request.sendall(struct.pack("!H", len(reply)) + reply)
        except OSError:
            return

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.request.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


class UDPServer(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = False


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------------------------------ firewall
def is_admin() -> bool:
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def firewall_commands(action: str, pcsx2: bool):
    """List of commands (each a list of args) for this OS. action: 'open' or 'close'."""
    osn = os_name()
    cmds = []
    if osn == "Windows":
        rules = [("DNS-UDP", "UDP", "53"), ("DNS-TCP", "TCP", "53")]
        if pcsx2:
            rules.append(("PCSX2-game-UDP", "UDP", GAME_UDP_PORTS))
        if action == "close":
            # the SCOPS window (V7) adds this one for its router test (UDP 3659)
            cmds.append(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={RULE}-router-test-UDP"])
        for label, proto, ports in rules:
            name = f"{RULE}-{label}"
            cmds.append(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
            if action == "open":
                cmds.append(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in",
                             "action=allow", f"protocol={proto}", f"localport={ports}", "profile=private,domain"])
    elif osn == "Linux":
        ports = [("53", "udp"), ("53", "tcp")] + ([(p, "udp") for p in ("3658", "10070:10080")] if pcsx2 else [])
        if shutil.which("ufw"):
            for p, proto in ports:
                cmds.append(["ufw"] + (["allow"] if action == "open" else ["delete", "allow"]) + [f"{p}/{proto}"])
        elif shutil.which("firewall-cmd"):
            flag = "--add-port" if action == "open" else "--remove-port"
            for p, proto in ports:
                cmds.append(["firewall-cmd", "--permanent", f"{flag}={p.replace(':', '-')}/{proto}"])
            cmds.append(["firewall-cmd", "--reload"])
        elif shutil.which("iptables"):
            for p, proto in ports:
                cmds.append(["iptables", "-I" if action == "open" else "-D", "INPUT", "-p", proto, "--dport", p,
                             "-m", "comment", "--comment", RULE, "-j", "ACCEPT"])
    elif osn == "macOS":
        fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        exe = os.path.realpath(sys.executable)
        if action == "open":
            cmds += [[fw, "--add", exe], [fw, "--unblockapp", exe]]
        else:
            cmds += [[fw, "--remove", exe]]
    return cmds


def manage_firewall(action: str, pcsx2: bool, dry_run: bool) -> bool:
    osn = os_name()
    cmds = firewall_commands(action, pcsx2)
    verb = "Opening" if action == "open" else "Removing"
    if not cmds:
        say("WARN", f"No supported firewall tool found on {osn}. If you use one, allow UDP and TCP port 53 yourself.")
        return False
    what = "DNS (UDP/TCP 53)" + (f" and PCSX2 game ports (UDP {GAME_UDP_PORTS})" if pcsx2 else "")
    say("INFO", f"{verb} firewall rules on {osn} for {what}.")
    if osn == "Linux" and cmds[0][0] == "iptables" and action == "open":
        say("INFO", "iptables rules last until reboot. Run with --open-firewall again after a restart.")
    if dry_run:
        for c in cmds:
            print("    " + " ".join(f'"{a}"' if " " in a else a for a in c))
        return True
    if not is_admin():
        if osn == "Windows":
            return _windows_elevated(cmds)
        say("ERROR", "Changing the firewall needs root. Run again with sudo, e.g.:")
        print(f"    sudo {sys.executable} {' '.join(sys.argv)}")
        return False
    ok = True
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        # deleting a rule that does not exist is fine
        if r.returncode != 0 and not (" delete" in " " + " ".join(c) or "-D" in c or "--remove" in " ".join(c)):
            ok = False
            say("ERROR", f"{' '.join(c)} failed: {(r.stderr or r.stdout).strip()}")
    say("INFO", "Firewall updated." if ok else "Some firewall commands failed (see above).")
    return ok


def _windows_elevated(cmds) -> bool:
    """Run the netsh commands in one elevated cmd.exe (one UAC prompt)."""
    import ctypes
    line = " & ".join(subprocess.list2cmdline(c) for c in cmds)
    say("INFO", "Windows will ask for administrator permission to change the firewall.")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f"/c {line}", None, 0)
    if rc <= 32:
        say("ERROR", "Firewall not changed (permission was refused). You can also allow it when Windows asks.")
        return False
    import time
    time.sleep(3)
    chk = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE}-DNS-UDP"],
                         capture_output=True, text=True)
    present = chk.returncode == 0 and "53" in chk.stdout
    say("INFO", "Firewall rules are in place." if present else "Could not confirm the firewall rules.")
    return present


# ------------------------------------------------------------------------------------------------- UPnP
# Same job as SCOPS_UPnP_Check: SSDP + SOAP, not the Windows COM helper.
# Forwards UDP 3658 and 10070-10080 to the PS2 address, refreshes every 20 minutes,
# tells the relay this host is open when the router's address is public, and removes
# the forwards on exit. A mapping to a PS2 address is often refused (secure mode).
GAME_PORTS = [3658] + list(range(10070, 10081))
MAP_DESC = "SCCT-PS2"
LEASE_S = 3600
UPNP_REFRESH_S = 20 * 60
RELAY_UDP = 45000
SSDP_STS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _xml_local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _soap_field(text: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}>\s*([^<]*)", text or "")
    return m.group(1).strip() if m else ""


def _ip_private(ip: str) -> bool:
    """True when this is not a public internet address (LAN, CGNAT, loopback, or empty)."""
    if not ip or ip == "0.0.0.0":
        return True
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return True
    return not addr.is_global


def _ps2_cfg_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "SCOPS", "upnp_ps2_address.txt")


def _read_saved_ps2() -> str:
    try:
        with open(_ps2_cfg_path(), encoding="utf-8") as f:
            line = f.readline().strip()
        ipaddress.IPv4Address(line)
        return line
    except (OSError, ValueError):
        return ""


def _save_ps2(ip: str) -> None:
    path = _ps2_cfg_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(ip + "\n")
    except OSError:
        pass


def _http(url: str, body: bytes | None = None, soap_action: str | None = None, timeout: float = 5) -> tuple:
    headers = {"User-Agent": "scopsDNS"}
    data = None
    if body is not None:
        headers["Content-Type"] = 'text/xml; charset="utf-8"'
        headers["SOAPAction"] = f'"{soap_action}"'
        data = body
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except OSError:
            text = ""
        return e.code, text
    except OSError as e:
        return -1, str(e)


def _find_igd(cands) -> dict:
    found = {}
    group = ("239.255.255.250", 1900)
    for cand in cands:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((cand, 0))
            sock.settimeout(0.3)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            except OSError:
                pass
            for st in SSDP_STS:
                q = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                     f"MAN: \"ssdp:discover\"\r\nMX: 2\r\nST: {st}\r\n\r\n")
                sock.sendto(q.encode("ascii"), group)
            end = time.time() + 2.5
            while time.time() < end:
                try:
                    data, _src = sock.recvfrom(4096)
                except OSError:
                    continue
                m = re.search(r"(?im)^location:\s*(\S+)", data.decode("ascii", "replace"))
                if m and m.group(1) not in found:
                    found[m.group(1)] = cand
        except OSError:
            pass
        finally:
            sock.close()
        if found:
            break
    return found


def _wan_service(location: str):
    code, text = _http(location)
    if code != 200 or not text:
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    base = location
    friendly = ""
    for el in root.iter():
        name = _xml_local(el.tag)
        if name == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
        elif name == "friendlyName" and (el.text or "").strip() and not friendly:
            friendly = el.text.strip()
    best = None
    for el in root.iter():
        if _xml_local(el.tag) != "service":
            continue
        stype, ctl = "", ""
        for child in el:
            n = _xml_local(child.tag)
            if n == "serviceType":
                stype = (child.text or "").strip()
            elif n == "controlURL":
                ctl = (child.text or "").strip()
        if not stype or not ctl or not re.search(r"WANIPConnection|WANPPPConnection", stype):
            continue
        svc = {"type": stype, "ctl": urllib.parse.urljoin(base, ctl), "name": friendly}
        if "WANIPConnection" in stype:
            return svc
        if best is None:
            best = svc
    return best


def _igd(svc, action: str, arg_xml: str, timeout: float = 5) -> dict:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{svc["type"]}">{arg_xml}</u:{action}></s:Body></s:Envelope>'
    ).encode("utf-8")
    code, text = _http(svc["ctl"], body, f'{svc["type"]}#{action}', timeout)
    if code == 200:
        return {"ok": True, "code": 0, "text": text, "desc": ""}
    err = code
    m = re.search(r"<(?:\w+:)?errorCode>\s*(\d+)", text or "")
    if m:
        err = int(m.group(1))
    desc = _soap_field(text, "errorDescription")
    if err == -1:
        desc = text
    return {"ok": False, "code": err, "text": text, "desc": desc}


def _external_ip(svc) -> str:
    r = _igd(svc, "GetExternalIPAddress", "")
    return _soap_field(r["text"], "NewExternalIPAddress") if r["ok"] else ""


def _get_mapping(svc, port: int, timeout: float = 5):
    r = _igd(svc, "GetSpecificPortMappingEntry",
             f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{port}</NewExternalPort><NewProtocol>UDP</NewProtocol>",
             timeout)
    if not r["ok"]:
        return None
    return {"client": _soap_field(r["text"], "NewInternalClient"),
            "desc": _soap_field(r["text"], "NewPortMappingDescription")}


def _remove_mapping(svc, port: int, timeout: float = 5) -> dict:
    return _igd(svc, "DeletePortMapping",
                f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{port}</NewExternalPort><NewProtocol>UDP</NewProtocol>",
                timeout)


def _explain(code: int, desc: str) -> str:
    known = {
        -1: f"no answer from the router ({desc})",
        401: "the router does not offer port forwarding through UPnP (401)",
        501: "the router refused the request (501 Action Failed)",
        606: "the router does not allow UPnP changes (606). UPnP may be set to read-only.",
        715: "the router does not accept forwards from any address (715)",
        716: "the router needs a different external port setting (716)",
        718: f"UDP port already forwarded to another device: {desc}",
        724: "the router only accepts the same external and internal port (724)",
        729: "the router refused: conflict with another forwarding setting (729)",
    }
    return known.get(code, f"router error {code} {desc}".strip())


def _add_mapping(svc, port: int, client: str) -> dict:
    lease = LEASE_S
    r = {"ok": False, "code": -1, "text": "", "desc": ""}
    for _try in range(3):
        arg = (
            f"<NewRemoteHost></NewRemoteHost><NewExternalPort>{port}</NewExternalPort><NewProtocol>UDP</NewProtocol>"
            f"<NewInternalPort>{port}</NewInternalPort><NewInternalClient>{client}</NewInternalClient>"
            f"<NewEnabled>1</NewEnabled><NewPortMappingDescription>{MAP_DESC}</NewPortMappingDescription>"
            f"<NewLeaseDuration>{lease}</NewLeaseDuration>"
        )
        r = _igd(svc, "AddPortMapping", arg)
        if r["ok"]:
            return r
        if r["code"] == 725 and lease != 0:
            lease = 0
            continue
        if r["code"] == 718:
            existing = _get_mapping(svc, port)
            if existing and (existing["client"] == client or existing["desc"].startswith("SCCT")):
                _remove_mapping(svc, port)
                continue
            if existing:
                r["desc"] = f"already forwarded to {existing['client']} ({existing['desc']})"
        return r
    return r


def _map_all(svc, client: str):
    ok, bad = [], []
    for port in GAME_PORTS:
        r = _add_mapping(svc, port, client)
        if r["ok"]:
            ok.append(port)
        else:
            bad.append(f"UDP {port}: {_explain(r['code'], r['desc'])}")
    return ok, bad


def _send_open_flag(server: str) -> bool:
    payload = b'{"op":"upnp_open"}'
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(2)
    try:
        sock.sendto(payload, (server, RELAY_UDP))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def choose_ps2(explicit: str, skip: bool) -> str:
    """PS2 address to map, or '' to leave UPnP off."""
    if skip:
        return ""
    if explicit:
        ipaddress.IPv4Address(explicit)
        return explicit
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    saved = _read_saved_ps2()
    print("== UPnP (hosting) ==")
    print("This opens the game ports on your router to the PS2 address (not this PC).")
    print("PCSX2 PCAP Bridged: Settings > Network & HDD > PS2 Address.")
    if saved:
        print(f"Last address: {saved}")
        print("Press Enter to use it, type a different address, or type skip.")
    else:
        print("Type the PS2 address (for example 192.168.1.240), or press Enter to skip.")
    try:
        line = input("PS2 address: ").strip()
    except EOFError:
        return ""
    if not line:
        return saved
    if line.lower() in ("skip", "no", "n", "-"):
        return ""
    ipaddress.IPv4Address(line)
    return line


class UpnpSession:
    def __init__(self, ps2: str, server: str):
        self.ps2 = ps2
        self.server = server
        self.svc = None
        self.mapped = []
        self.public = False
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        say("INFO", f"Looking for a UPnP router to forward UDP 3658 and 10070-10080 to {self.ps2}...")
        pc = get_ip()
        if self.ps2 == pc:
            say("WARN", f"{self.ps2} is this PC's address. PCAP Bridged uses the PS2 Address, not the PC. "
                        "Sockets mode is the case that uses the PC's address.")
        found = _find_igd([pc] + [a for a in all_ips() if a != pc])
        if not found:
            say("WARN", "No UPnP router answered. Turn UPnP on, or forward those ports to the PS2 by hand.")
            say("INFO", "You can still host through the SCOPS relay. Players may wait longer at a map change.")
            return
        svc = None
        for loc in found:
            svc = _wan_service(loc)
            if svc:
                break
        if not svc:
            say("WARN", "A UPnP device answered, but it does not offer port forwarding.")
            return
        self.svc = svc
        name = svc["name"] or "your router"
        ext = _external_ip(svc)
        ok, bad = _map_all(svc, self.ps2)
        self.mapped = ok
        if not ok:
            why = bad[0] if bad else "no ports mapped"
            say("WARN", f"{name} answered UPnP but would not forward to {self.ps2}: {why}")
            say("WARN", "Many routers only map a port to the PC that asked (secure mode). "
                        "Forward UDP 3658 and 10070-10080 to the PS2 by hand, or turn that off.")
            say("INFO", "You can still host through the SCOPS relay.")
            return
        self.public = not _ip_private(ext)
        say("INFO", f"{name} forwarded {len(ok)} of {len(GAME_PORTS)} UDP ports to {self.ps2}.")
        if self.public:
            say("INFO", f"Router's internet address is public ({ext}).")
            if _send_open_flag(self.server):
                say("INFO", f"Told the SCOPS server ({self.server}:{RELAY_UDP}) this host is open.")
            else:
                say("WARN", "Could not send the open flag to the SCOPS server. The forwards are still in place.")
        else:
            shown = ext or "none reported"
            say("WARN", f"Ports are open on this router, but its internet address is {shown} (not public).")
            say("WARN", "A second router or CGNAT is in the way, so players on the internet still cannot reach "
                        "the PS2 directly. The relay still works. The server was not told you are open.")
        for line in bad:
            say("WARN", line)
        _save_ps2(self.ps2)
        if self.mapped:
            self._thread = threading.Thread(target=self._refresh, daemon=True)
            self._thread.start()

    def _refresh(self) -> None:
        while not self._stop.wait(UPNP_REFRESH_S):
            if not self.svc:
                return
            try:
                ok, bad = _map_all(self.svc, self.ps2)
                if ok:
                    self.mapped = ok
                say("INFO", f"UPnP forwards refreshed ({len(ok)} ports).")
                for line in bad:
                    say("WARN", line)
                if self.public:
                    _send_open_flag(self.server)
            except Exception as e:
                say("WARN", f"UPnP refresh failed: {e}")

    def close(self) -> None:
        self._stop.set()
        if not self.svc or not self.mapped:
            return
        say("INFO", "Removing UPnP forwards...")
        for port in list(self.mapped):
            try:
                existing = _get_mapping(self.svc, port, timeout=2)
                if existing and existing["desc"].startswith("SCCT"):
                    _remove_mapping(self.svc, port, timeout=2)
            except Exception:
                pass
        self.mapped = []


# ------------------------------------------------------------------------------------------------ main
def check_server(ip: str) -> None:
    try:
        with socket.create_connection((ip, LOBBY_TCP_PORT), timeout=4):
            say("INFO", f"SCOPS server {ip} is reachable (TCP {LOBBY_TCP_PORT}).")
    except OSError as e:
        say("WARN", f"Could not reach the SCOPS server {ip} on TCP {LOBBY_TCP_PORT} ({e}).")
        say("WARN", "The server may be down, or something on your network blocks it.")


def banner(ip: str, cfg) -> None:
    def p(line=""):
        print(line, flush=True)
    p("+===================================+")
    p("|          SCOPS DNS Server         |")
    p(f"|            Version {VERSION}            |")
    p("+===================================+")
    p()
    p("== Welcome to scopsDNS! ==")
    p("Splinter Cell Chaos Theory (PS2) online. DNS for a blocked provider, and UPnP when you host.")
    p()
    p("== How To Use ==")
    p("Make sure your PS2 (or the PC running PCSX2) is on the same network as this computer.")
    p()
    p("Put these settings in for DNS on your PS2 / in PCSX2:")
    p("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    p(f"Primary DNS:   {format_ip(ip)}")
    p(f"Secondary DNS: {format_ip(ip)}")
    p("~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~")
    p("(Use this computer for both. A public DNS as secondary would send the game to Ubisoft's old servers.)")
    others = [a for a in all_ips() if a != ip]
    if ip == "127.0.0.1":
        print("WARNING: no network connection found. Connect this computer to your home network and restart scopsDNS.")
    elif others:
        print("This computer has more than one network address. If the console is on a different one, use that instead:")
        print("    " + ", ".join(format_ip(a) for a in others))
    if ip != "127.0.0.1" and not ipaddress.IPv4Address(ip).is_private:
        print("NOTE: this computer has a public address. The console must be able to reach it directly.")
    print()
    print(f"Game lookups are answered with the SCOPS server {cfg.server}; everything else goes to "
          f"{', '.join(cfg.upstream)}.\n")


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Local DNS server for Splinter Cell Chaos Theory PS2 online (SCOPS)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help=f"SCOPS server address (default {DEFAULT_SERVER})")
    ap.add_argument("--upstream", action="append", help="resolver for all other names (repeatable)")
    ap.add_argument("--listen", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--ip", help="this computer's address to show the PS2 (the network card picked in the SCOPS window); "
                                 "default: the card that carries internet traffic")
    ap.add_argument("--port", type=int, default=53, help="port (consoles need 53; others only for testing)")
    ap.add_argument("--no-tcp", action="store_true", help="UDP only")
    ap.add_argument("--verbose", action="store_true", help="also log names that are forwarded")
    ap.add_argument("--no-check", action="store_true", help="skip the start-up server reachability and DNS hijack checks")
    ap.add_argument("--check-only", action="store_true",
                    help="only test for ISP DNS hijack, then exit")
    ap.add_argument("--open-firewall", action="store_true",
                    help="allow DNS (UDP/TCP 53) through this computer's firewall (on by default on Windows)")
    ap.add_argument("--pcsx2-ports", action="store_true",
                    help=f"also open UDP {GAME_UDP_PORTS} for PCSX2 in Sockets mode on this computer")
    ap.add_argument("--no-firewall", action="store_true",
                    help="do not open or change any firewall rules")
    ap.add_argument("--close-firewall", action="store_true", help="remove the rules added by scopsDNS and exit")
    ap.add_argument("--upnp", metavar="PS2", help="open router ports to this PS2 address (no prompt)")
    ap.add_argument("--no-upnp", action="store_true", help="DNS only; do not ask about UPnP")
    ap.add_argument("--dry-run", action="store_true", help="with firewall changes: only print the commands")
    ap.add_argument("--find-ps2", action="store_true",
                    help="look for the PS2 on this computer's network (Sony MAC address) and print its address, then exit")
    cfg = ap.parse_args(argv)
    try:
        ipaddress.IPv4Address(cfg.server)
    except ValueError:
        print(f"--server must be an IPv4 address, got {cfg.server}")
        return 2
    cfg.upstream = cfg.upstream or list(DEFAULT_UPSTREAMS)

    if cfg.close_firewall:
        return 0 if manage_firewall("close", True, cfg.dry_run) else 1

    ip = get_ip()
    if cfg.ip:
        mine = set(all_ips()) | {ip}
        if cfg.ip in mine:
            ip = cfg.ip
        else:
            say("WARN", f"{cfg.ip} is not an address of this computer (it has {', '.join(sorted(mine))}); "
                        f"showing {ip} instead.")
    if cfg.find_ps2:
        return report_find_ps2(ip)
    if cfg.check_only:
        say("INFO", f"This computer's address: {format_ip(ip)}")
        detect_dns_hijack(cfg.upstream, cfg.server)
        return 0

    banner(ip, cfg)
    say("INFO", f"Detected operating system: {os_name()}")
    # On Windows the console cannot query this computer unless inbound DNS is allowed.
    # Open only the DNS rules by default. The game ports are aimed at the PS2 address,
    # not this PC, so they stay opt-in (--pcsx2-ports). --no-firewall skips everything.
    open_fw = cfg.open_firewall or (os_name() == "Windows" and not cfg.no_firewall)
    pcsx2 = cfg.pcsx2_ports
    if cfg.no_firewall:
        open_fw = False
    if open_fw:
        manage_firewall("open", pcsx2, cfg.dry_run)
        if cfg.dry_run:
            return 0
    elif os_name() == "Windows":
        say("INFO", "Firewall rules not changed (--no-firewall). If the PS2 cannot reach this computer, "
                    "allow scopsDNS on Private networks when Windows asks.")
    if cfg.check_only:
        say("INFO", f"This computer's address: {format_ip(ip)}")
        detect_dns_hijack(cfg.upstream, cfg.server)
        return 0

    if not cfg.no_check:
        check_server(cfg.server)
        detect_dns_hijack(cfg.upstream, cfg.server)

    upnp = None
    if not cfg.dry_run:
        try:
            ps2 = choose_ps2(cfg.upnp, cfg.no_upnp)
        except ValueError:
            say("ERROR", "That is not an IPv4 address. Starting DNS only.")
            ps2 = ""
        if ps2:
            upnp = UpnpSession(ps2, cfg.server)
            upnp.start()
        elif not cfg.no_upnp and not cfg.upnp:
            say("INFO", "UPnP skipped. DNS only.")

    servers = []
    if cfg.listen == "0.0.0.0":
        # e.g. Ubuntu's systemd-resolved holds 127.0.0.53:53, which blocks 0.0.0.0:53; this computer's own address works
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("0.0.0.0", cfg.port))
            probe.close()
        except OSError as e:
            if e.errno in (48, 98, 10048) and ip != "127.0.0.1":  # address already in use (macOS, Linux, Windows)
                say("INFO", f"Port {cfg.port} is busy on all addresses; listening on {ip} only.")
                cfg.listen = ip
    try:
        udp = UDPServer((cfg.listen, cfg.port), UDPHandler)
        udp.cfg = cfg
        servers.append(udp)
        if not cfg.no_tcp:
            try:
                tcp = TCPServer((cfg.listen, cfg.port), TCPHandler)
                tcp.cfg = cfg
                servers.append(tcp)
            except OSError as e:
                say("WARN", f"TCP port {cfg.port} not available ({e}); continuing with UDP only (enough for the PS2).")
    except PermissionError:
        say("ERROR", f"Cannot listen on UDP port {cfg.port}.")
        if os_name() == "Windows":
            say("ERROR", "Port 53 is already in use, or this program is not allowed to take it.")
            say("ERROR", "Close any other scopsDNS window, then open this one again. If it still fails, right-click Start scopsDNS.bat and Run as administrator.")
        else:
            say("ERROR", "Run this as administrator (Windows) or with sudo (macOS/Linux).")
        if upnp:
            upnp.close()
        return 1
    except OSError as e:
        say("ERROR", f"Cannot listen on UDP port {cfg.port}: {e}")
        if os_name() == "Windows":
            say("ERROR", "Another program already uses port 53 (often 'Internet Connection Sharing').")
        else:
            say("ERROR", "Run with sudo, and check what uses the port: sudo lsof -i :53")
            if os_name() == "Linux":
                say("ERROR", "On Ubuntu, systemd-resolved listens on 127.0.0.53 only; use --listen <this computer's IP>.")
        if upnp:
            upnp.close()
        return 1

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    say("INFO", "scopsDNS is ready. Waiting for DNS requests from your console... (Ctrl+C to stop)")
    try:
        stop = threading.Event()
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print()
        say("INFO", "Stopped.")
    finally:
        if upnp:
            upnp.close()
        for s in servers:
            s.shutdown()
            s.server_close()
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    if getattr(sys, "frozen", False) and code != 0 and os.name == "nt":
        # double-clicked .exe: keep the window open so the message can be read
        input("\nPress Enter to close this window...")
    sys.exit(code)
