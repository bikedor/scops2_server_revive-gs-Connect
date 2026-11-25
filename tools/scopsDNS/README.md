# scopsDNS 2.2 / SCOPS window V8.19

## Quick start
1. Unzip this folder anywhere (Desktop is fine) and double-click **SCOPS.exe**.
   Windows may say "Windows protected your PC" / unrecognized app: the program is not code-signed.
   Click **More info -> Run anyway**. If your antivirus blocks the exe, use **SCOPS_UPnP_Check.bat** instead (same window).
2. **DNS** (everyone): follow the top of the window. Green check = put **38.49.212.120** in both DNS entries.
3. **Hosting (UPnP)** (only if you host): pick what you host with, then press **Check UPnP**.
   - **Real PS2** or **PCSX2 PCAP Bridged**: press **Find my PS2** (game at the online menu). Or type the address.
   - **PCSX2 Sockets**: pick that. The address fills with this PC. Windows asks once to allow UDP 3658 and 10070-10080.
   - **PS3 with PS2 support (untested)**: pick that and press **Find my PS3**. Only early PS3s play PS2 games (CECHA/CECHB fully, CECHC/CECHE partly). Find lists every Sony device that is not a PS2 (a PS4 or PS5 counts too), so check it picked the PS3. Hosting from a PS3 has not been tested on SCOPS yet.
   - **Copy** puts the address on the clipboard.
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

## What changed in V8.19
- **DNS check no longer says "no answer" when DNS works.** The window read only the first part of the check's output, so on PCs without scopsDNS.exe or Python (where `scopsDNS.ps1` runs) the result line was often never seen and the check turned yellow. It now reads everything, and reads `scops-dns.log` once more before giving up. The same fix lets the Log show every console lookup while local DNS runs.

## What changed in V8.18
- **Hosting with** picker: Real PS2, PCSX2 PCAP Bridged, PCSX2 Sockets, or PS3 with PS2 support (untested). Sockets still locks the address to this PC and opens the Windows firewall. The other three use Find.
- **Find my PS3.** A PS3 that plays PS2 games uses its own network adapter, not a PS2-era ID, so Find looks for any later Sony ID. That includes a PS4 or PS5, so the window lists what it found and asks you to check.
- **Find is faster.** It first uses devices Windows has seen in the last moments, and scans the network if the console is not among them (older entries are not trusted: the console may have a new address).
- **Copy** next to the address.
- **Close warning.** If the window is holding forwards, closing it asks before it removes them (not when Windows shuts down or signs out).
- After a green check, the first line says the one next step: leave the window open, and which DNS address to put in.

## What changed in V8.17
- **Find my PS2** (next to the PCSX2 Sockets box). For a real PS2 or PCSX2 PCAP Bridged: start the game's online menu on the PS2, then press it. The window checks every address on your network for a Sony network adapter (MAC starting 00:04:1F for the PS2 network adapter / PCSX2, or 00:13:15 / 00:15:C1 for a slim PS2; other Sony prefixes are listed as "other Sony device") and fills in the PS2 address. No router login needed, no admin rights. Turned off while the Sockets box is ticked.
- **The local DNS fills it in by itself.** When the PS2 uses this PC as its DNS, its first lookup shows its address ("PS2 address seen" in the Log), and an empty PS2 address box is filled with it.
- scopsDNS.py: `--find-ps2` prints the same search result (Windows, macOS, Linux). Mac: see README_MAC.txt (menu option 7, and option 3 suggests the address it finds).

## What changed in V8.16
- **PCSX2 Sockets checkbox.** Tick it and the address becomes this PC's address (the network card picked above) and cannot be typed over. Untick it and the last PS2 address comes back.
- On Check UPnP with the box ticked, Windows is asked once to allow UDP 3658 and 10070-10080 on this PC (rule `scopsDNS-PCSX2-game-UDP`). PCAP Bridged and a real PS2 do not get that rule. **Remove firewall rules.bat** still deletes it.
- The Log report has a `Sockets: yes/no` line. The window title says **SCOPS V8.16**.
- **Old scopsDNS.exe is skipped.** An exe built from an older scopsDNS.py has no `--check-only` and made every check fail ("unrecognized arguments", exit 2). The window now asks the exe for `--help` once and skips it if `--check-only` is missing (Python or the built-in `scopsDNS.ps1` is used instead; the Log report says so). Rebuild the exe from the current scopsDNS.py with build_exe.bat.
- **One network card fixed.** On a PC with a single network card the Sockets box said "No network card" (PowerShell 5.1 returns one card without a count). The card list is now always a list, which also fixes the picked-card address shown while the local DNS runs.
- **Public network warning** (Sockets mode): if Windows has the network set to Public, the Sockets firewall rule does not apply. Check UPnP says how to set it to Private.
- **Remove firewall rules.bat** tries scopsDNS.exe first, then Python, then removes the rules directly (it used to stop at the Windows Store python placeholder).

## What changed in V8.15
- **No Python needed.** The DNS check and the local DNS now also come as `scopsDNS.ps1`, a copy of `scopsDNS.py` that runs on the Windows PowerShell built into every Windows 10/11. Same tests, same results. The window uses `scopsDNS.exe` if it is in the folder, then a real Python with `scopsDNS.py`, and otherwise `scopsDNS.ps1`. Players just double-click SCOPS.exe.
- The Windows Store `python.exe` placeholder ("Python was not found", exit 9009) is skipped: every Python the window finds is test-run first (2026-11-20: a PC without Python showed "DNS check gave no answer").
- The Log report says which DNS tool ran (`scopsDNS.exe`, `scopsDNS.py (Python)` or `scopsDNS.ps1 (built-in)`).
- **First-launch "no answer" fixed.** A fast check (under a second) could finish before the window read its result; the window called it "no answer", started a second try and deleted the first result. It now keeps reading for 2 s after the check ends.
- `Test DNS check (built-in).bat`: runs the built-in check in a window and shows the result, for testing.
- The built-in local DNS answers on UDP 53 only (what the PS2 uses); the Python and exe versions also answer TCP 53.

## What changed in V8.14
- **Log** button (idea from PSRewired's RetroDNS Log tab). It opens a log window that updates every 2 seconds: the DNS result, your network cards, the UPnP result and router, and the last lines of the window's two logs (`scops-dns.log`, `scops-window.log`).
- If something does not work, press **Log**, then **Copy all**, and paste (Ctrl+V) in Discord. Copy all also saves it as `scops-report.txt` next to SCOPS.exe. **Open folder** opens that folder.
- The log sends nothing anywhere by itself. Your public address is shortened to its first two parts (for example 162.202.x.x) and your Windows user name is hidden. Home-network addresses (192.168..., 10...) and the SCOPS server address stay whole because they are what we need to see.

## What changed in V8.13
- **Only one SCOPS window at a time.** Opening it again (for example double-clicking SCOPS.exe twice while it is still starting) now says "already open" instead of starting a second window. A second window used to close the first window's DNS check, which then showed no answer until the app was reopened.
- **No more silent DNS check.** If the check ends or hangs (40 s) without an answer, the window checks once more by itself, then shows "DNS check gave no answer" with a **Check again** button.

## What changed in V8.12
- **PC network card** list in the DNS section (idea from PSRewired's RetroDNS): every card of this PC that is up, with its address. Your Wi-Fi or Ethernet card that carries the internet is picked first; VPN and virtual adapters (ZeroTier, Tailscale, Hamachi, TAP/TUN, Hyper-V, VirtualBox, VMware, Bluetooth, ...) are listed last and marked "the PS2 cannot use this".
- With local DNS on, the address shown for the PS2 is the picked card's address. Changing the card updates it at once (local DNS answers on every card, so nothing restarts).

## What changed in V8.11
- Red ("DNS is being redirected") now also when your **router** answers DNS itself (a reply from another home-network address): the PS2 sits behind the same router, so it is affected too.
- Amber ("Something else answers DNS here") when the reply comes from this PC, is forged in the SCOPS server's name, or even 192.0.2.1 answers. From the PC alone that can be a program on the PC or your provider, so the window says: put 38.49.212.120 in the PS2 first, and use local DNS only if the game does not get online.
- Wrong answers no longer show as "SCOPS DNS did not answer"; that headline is now only for no reply at all.

## What changed in V8.10
- A red "DNS is being redirected" now means a query sent to the SCOPS server was answered by another internet DNS server. That is when a PS2 set to 38.49.212.120 will not work, and local DNS is the fix.
- A reply from this PC (a VPN, antivirus or DNS filter) or an answer to the 192.0.2.1 canary is amber, "This PC filters DNS". Put 38.49.212.120 in the PS2 first. Use local DNS only if the game does not get online.
- If the SCOPS server refuses a test name but a game name did not come back, the window says to try again instead of calling it a hijack.

## What changed in V8.9
- If the normal DNS check fails, three extra checks find out why:
  - a DNS question to 192.0.2.1 (an address where no DNS server can exist): any reply means every DNS question on this network or PC is being answered on the way;
  - a made-up name sent to the SCOPS DNS: the real server refuses it, so any other answer means another DNS server answered instead;
  - "who are you" (version.bind / id.server): the answering server often names itself, and the window shows that name.
- Headline is now "DNS is being redirected", with the reason. If the server itself is reachable but the check failed, it says to try again instead.

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
