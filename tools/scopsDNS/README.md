# scopsDNS 2.1 / SCOPS window V8.8

## Quick start
1. Unzip this folder anywhere (Desktop is fine) and double-click **SCOPS.exe**.
   Windows may say "Windows protected your PC" / unrecognized app: the program is not code-signed.
   Click **More info -> Run anyway**. If your antivirus blocks the exe, use **SCOPS_UPnP_Check.bat** instead (same window).
2. **DNS** (everyone): follow the top of the window. Green check = put **38.49.212.120** in both DNS entries.
3. **Hosting (UPnP)** (only if you host): type the PS2 address and press **Check UPnP**.
   - PCSX2 in **PCAP Bridged** mode: Settings > Network & HDD > **PS2 Address** (e.g. 192.168.1.240).
   - PCSX2 in **Sockets** mode: **this PC's own address**.
   - Real PS2: the console's address (its network settings, or the router's device list).
   - Turn any **VPN off** while hosting.
4. Leave the window open while you host; close it when you are done (that removes the forwards).
   With more than one joiner, have each wait 20-30 seconds after the previous one got in (unless the host is on a
   public address, e.g. a PS2 plugged straight into the modem).
5. Please send a screenshot of the window after the **Router test** line appears. Every router brand tells us more.

UPnP is an extra, not a requirement, and it does not help on every router (on some, the router still drops the game's
connections after a while even with the ports forwarded). Without it, or if it does not help, you can still host - the
SCOPS server relay gets players to you; returns after a map change may just take a little longer.

Best if you can: forward UDP 3658 and 10070-10080 to the PS2 address by hand (press **Open my router**).
DMZ only as a last resort: use it for a PS2 console, never a PC (it opens every port), and turn it off after playing.

## What changed in V8.8
- DNS check: tries up to 3 times (two names) and only says "redirected" when two answers are both wrong, so one lost packet no longer shows red.
- The red DNS message says what came back and from where, and that a PS2 that already gets online can ignore it (the redirect is then on this PC: a VPN, antivirus or DNS filter).

## What changed in V8.7
- DMZ wording: only for a PS2 console, never a PC, and turn it off after playing.
- README: the quick start now says hand forward first, DMZ last; older notes no longer say the opposite.
- The ASUS line on a green check now also works before 'Guide for my router' has been pressed, and says to close the window before making a hand forward.

## What changed in V8.6
- DMZ is now mentioned only as a last resort (V8.7 sharpened the wording). The hand-made forward is still the better path.
- On a green check, if the router looks like an ASUS, the window adds one line: a hand-made forward keeps used holes alive where UPnP does not, and points at Open my router.

## What changed in V8.5
- Router detection now also works on routers whose start page forwards the browser by script (ASUS), and asks the
  router for its maker and model over UPnP if the page does not say (read only; no forward is added).

## What changed in V8.4
- The window now tries to tell which router you have: from the router's UPnP answer (maker and model) after
  Check UPnP, or else from your router's own login page. After you press **Open my router**, a
  **Guide for my router** button opens the matching step-by-step guide on portforward.com
  (ASUS, TP-Link, Netgear, Linksys, D-Link, Arris, Technicolor, Sagemcom), or portforward.com's full router list
  when the maker is not recognised. Nothing about your router is sent anywhere; the check only reads your router's page.

## What changed in V8.3
- New **Open my router** button at the bottom of the window (shown as soon as the window finds your router).
  It opens your router's settings page in the browser and shows, in the window, exactly which ports to forward
  by hand to the PS2 address you typed: **UDP 3658** and **UDP 10070-10080**. You need the router's login.
- Why: on some routers (seen on an ASUS RT-AX86U) a forward made by hand keeps working where the UPnP one does not.
  A hand-made forward is the best option if you can do it (DMZ only as a last resort, see the top), and on ASUS leave
  hardware (NAT) acceleration on.

## What changed in V8.2
- After each forward the window reads the mapping back from the router. **Green only when every game port is confirmed on the PS2 address you typed.**
- If the router points the forward at this PC instead (some routers do this with UPnP secure mode on) the window goes **amber**, shows the address the router actually used, and does **not** tell the SCOPS server you are open. Those forwards are removed.
- If the router accepts the request but will not confirm where the port points, that is amber too, not green, and that forward is removed as well.
- Secure mode on ASUS: WAN > Internet Connection > "Enable secure UPnP IGD mode" = No.
- Renewal only re-notifies the server when every port still reads back on the PS2. The window title says **SCOPS V8.2**.

## What changed in V8
- Forwards are now really **renewed every 20 minutes** (V7's renewal could fail silently on some routers - seen on an
  ASUS - so the forwards ran out after an hour). If a renewal fails, the window looks for the router again and retries;
  each renewal shows a line like `[01:44] forwards renewed for another hour (12 of 12 ports)`, or **RENEWAL FAILED**
  with the router's error.
- If your router has a scheduled **UPnP restart**, it can drop the forwards until the next renewal (up to 20 minutes).
  Turning that schedule off avoids it.


One program for Splinter Cell Chaos Theory PS2 online (SCOPS): a local DNS server, and UPnP host-port
forwarding. Built on the idea of
[insigniaDNS](https://github.com/insignia-live/insigniaDNS) / [sudomemoDNS](https://github.com/Sudomemo/sudomemoDNS).

**DNS:** some internet providers, apartment networks and routers block or rewrite DNS to outside servers, so
setting your PS2 or PCSX2 DNS to **38.49.212.120** never works. scopsDNS runs on a computer in your home and answers
the game's lookups itself with the SCOPS server address.

**UPnP:** the window asks for the PS2 address and opens UDP 3658 and 10070-10080 to that address on the
router. Green check ("Ports forwarded") only when the router confirms those ports point at the PS2 address you typed. Amber when the router points them at this PC or will not confirm (those forwards are removed). If the router's internet address is public **and** the ports were confirmed, the
window tells the SCOPS server this host has forwards (logged only). Forwards are renewed every 20 minutes and removed when you
close the window. The address is the PS2 / PCSX2 PS2 Address, not this PC, unless PCSX2 is in Sockets mode.

| Names | Answer |
|---|---|
| `*.ubisoft.com`, `*.ubi.com`, `*.gamespy.com`, `*.gamespy.net`, `*.dnas.playstation.org`, `noname` (the game uses `gsconnect.ubisoft.com` and `gate1.us.dnas.playstation.org`) | **38.49.212.120** (the SCOPS server) |
| everything else | forwarded to 1.1.1.1, then 8.8.8.8, then 9.9.9.9 |

Differences from insigniaDNS: it also forwards all other names (so one DNS address is enough), the game list is built in
(no download at start-up), it checks that the SCOPS server is reachable, it probes for ISP DNS hijacking with a canary
NXDOMAIN query, it can open your firewall, and it needs only Python itself (no `pip install`). UDP and TCP port 53.

## Windows
1. Install Python 3 from python.org (tick **Add python.exe to PATH**), or put `scopsDNS.exe` in this folder
   (see "Build an .exe").
2. Double-click **Start scopsDNS.bat**. One window opens. No console.
   - **DNS** at the top. A green check means your provider is not blocking the SCOPS DNS: put **38.49.212.120** in both DNS entries on the PS2. A red X means it is blocked or the answer was wrong: press **Use local DNS** and put this computer's address in instead.
   - **Hosting (UPnP)** underneath. Type the PS2 address and press Check UPnP. Green only when the router confirms the ports point at that address; amber if it rewrites them onto this PC. Leave the window open while you host.
3. The window shows `Primary DNS: 192.168.001.113` (example). Put that address in as **Primary and Secondary DNS**
   on the PS2 network settings or in PCSX2.
4. Keep the window open while you play. A game lookup shows as
   `Console at 192.168.1.218 looked up gsconnect.ubisoft.com -> 38.49.212.120 (SCOPS)`.
   If UPnP succeeded on a public address, the server is told this host is open.

UPnP opens those ports on the **router**, aimed at the PS2 address. The Windows firewall rule for the same ports is separate and still opt-in (`--pcsx2-ports`), because a real PS2 and PCSX2 PCAP Bridged do not receive game traffic on the Windows IP.

### Router test
After a green UPnP check the window runs a one-minute router test. It forwards UDP **3659** to this PC (not the PS2),
listens on it, and asks the SCOPS server to send three small test packets: two from server ports your network has never
talked to, and one 45 s later from a port this PC has just answered on. The window shows what arrived, for example
"2 of 2 new connections from the internet reached this PC - the forward works", and tells the server. The test forward
is removed when the test ends.

The first time, **Windows asks once** to let the window receive the test (a firewall rule named
`scopsDNS-router-test-UDP`, private networks only). Say no and the test is skipped; hosting is not affected.
The server only records the result for now (log only); it does not change how players are routed.

To undo the firewall rules: **Remove firewall rules.bat**. It also removes the router-test rule. Close the window to remove the UPnP forwards.

## macOS / Linux
```
chmod +x start_scopsDNS.sh
./start_scopsDNS.sh --open-firewall      # first time (add --pcsx2-ports if PCSX2 runs on this computer)
./start_scopsDNS.sh                      # afterwards
./start_scopsDNS.sh --close-firewall     # remove the rules
```
Port 53 needs root, so the script uses `sudo`.
- **Linux:** uses `ufw`, `firewalld` or `iptables`, whichever is installed (iptables rules last until reboot).
  If systemd-resolved holds port 53, scopsDNS listens on this computer's own address instead, which is what the
  console uses anyway.
- **macOS:** allows Python through the built-in application firewall.
- **PCSX2 with Sockets on Linux:** you can skip scopsDNS. In PCSX2's network settings set DNS to **Internal** and add
  hosts `gsconnect.ubisoft.com` and `gate1.us.dnas.playstation.org` -> `38.49.212.120`.

## Good to know
- The console (or the PC running PCSX2) must be on the same network as this computer, and this computer must stay on.
- Give this computer a fixed address (DHCP reservation in the router) so the DNS you typed keeps working.
- Use this computer for **both** DNS entries. A public DNS as secondary would send the game to Ubisoft's old servers.
- scopsDNS does not change CGNAT. If UPnP works but the router's address is private, the ports are still opened
  and the server is not told you are open. Players behind CGNAT can still join through the relay.
- On start it sends a canary query (`scops-canary-....invalid`) to the public resolvers. A clean answer is NXDOMAIN.
  If an ISP is rewriting DNS, the tool prints a warning. Game names are still answered locally either way.

## Options
```
--server IP         SCOPS server (default 38.49.212.120)
--upstream IP       resolver for all other names (repeatable)
--open-firewall     allow DNS (UDP/TCP 53) in through this computer's firewall (on by default on Windows)
--pcsx2-ports       also open UDP 3658 and 10070-10080 (only for PCSX2 in Sockets mode on this computer)
--no-firewall       do not open or change any firewall rules
--close-firewall    remove those rules and exit
--dry-run           with firewall changes: only print the commands
--upnp PS2          open router ports to this address, without asking
--no-upnp           DNS only; do not ask about UPnP
--verbose           also log names that are forwarded
--no-check          skip the start-up server reachability and DNS hijack checks
--no-tcp            UDP only
--listen IP / --port N   listen address / port (consoles need port 53)
```

## Build an .exe (optional)
Run **build_exe.bat** once on a Windows PC with Python. It produces `scopsDNS.exe`. The Start bat uses `scopsDNS.py`
when Python is installed, and the exe only if Python is not.

## Credits
Idea and console instructions from insigniaDNS (Insignia) and sudomemoDNS (Team Sudomemo, MIT licence, see
`LICENSE-sudomemoDNS-insigniaDNS.txt`). Not affiliated with Ubisoft, Sony, Insignia or Sudomemo.
