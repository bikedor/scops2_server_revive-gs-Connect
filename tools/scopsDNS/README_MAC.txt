SCOPS for Mac - Splinter Cell Chaos Theory PS2 online
=====================================================

First time
1. Unzip the folder (Desktop is fine). Keep "SCOPS Mac.command" and "scopsDNS.py" together.
2. Right-click (or Control-click) "SCOPS Mac.command" and choose Open, then Open again.
   (macOS asks this once because the file is not from the App Store. A plain double-click works after that.)
   If macOS still refuses: System Settings > Privacy & Security, scroll down, click "Open Anyway".
3. If a box asks to install the "command line developer tools", click Install (this gives the Mac Python 3),
   wait for it to finish, then open SCOPS Mac.command again.

Playing (everyone)
- Choose 1 (Check my DNS).
  - "No changes needed" / no hijack: in the PS2 / PCSX2 network settings set DNS to manual and type
    38.49.212.120 in BOTH DNS boxes. Done, you can close the window.
  - DNS redirected / hijacked: choose 2 (Run local DNS), type the Mac address shown at the top of the menu
    in BOTH DNS boxes, and keep that window open while you play.

Hosting (optional)
- Real PS2, or PCSX2 in PCAP Bridged mode: start the game's online menu on the PS2, then choose 3.
  It looks for the PS2 on your network and suggests its address: press Return to use it, or type another.
  Option 7 only looks for the PS2 and shows its address.
- PCSX2 in Sockets mode on this Mac: choose 4. If macOS asks whether PCSX2 may accept incoming connections,
  click Allow.
- Keep the window open while you host. Ctrl+C stops it and closes the router ports again.
- Forwarding UDP 3658 and 10070-10080 by hand in the router works as well as or better than UPnP.

Options 2-5 ask for your Mac login password (port 53 and the firewall need it). Nothing shows while you type.

If something does not work: choose 6 (copies the last report) and paste it in Discord with Cmd+V.
The report is also saved as scops-mac-report.txt in this folder.
