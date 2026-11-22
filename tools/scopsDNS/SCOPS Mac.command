#!/bin/bash
# SCOPS for macOS - Splinter Cell Chaos Theory PS2 online (SCOPS server 38.49.212.120).
# Double-click this file. It opens Terminal with a small menu around scopsDNS.py (same checks as the Windows window).
# Works with the macOS bash 3.2 and the python3 that comes with Apple's Command Line Tools.

cd "$(dirname "$0")" || exit 1
SERVER="38.49.212.120"
LOG="$PWD/scops-mac-report.txt"

say() { printf '%s\n' "$*"; }
line() { say "------------------------------------------------------------"; }

find_python() {
    for p in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
        if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
            PY="$(command -v "$p")"
            return 0
        fi
    done
    return 1
}

mac_ip() {
    local ifc ip
    ifc="$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')"
    [ -n "$ifc" ] && ip="$(ipconfig getifaddr "$ifc" 2>/dev/null)"
    [ -z "$ip" ] && ip="$(ipconfig getifaddr en0 2>/dev/null)"
    [ -z "$ip" ] && ip="$(ipconfig getifaddr en1 2>/dev/null)"
    printf '%s' "$ip"
}

valid_ip() {
    [[ "$1" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
    local IFS=.
    for o in $1; do [ "$o" -le 255 ] || return 1; done
    return 0
}

keep_log() {
    # Ctrl+C must reach scopsDNS.py only (it closes the UPnP ports on the way out), not the copy to the report.
    ( trap '' INT; exec tee -a "$LOG" )
}

run_logged() {
    # Runs scopsDNS.py, shows its output and keeps a copy in scops-mac-report.txt (replaced each run).
    {
        say "SCOPS Mac report - $(date '+%Y-%m-%d %H:%M:%S')"
        say "macOS $(sw_vers -productVersion 2>/dev/null) - $("$PY" --version 2>&1) - menu choice: $CHOICE"
        say "Command: scopsDNS.py $*"
        line
    } > "$LOG"
    if [ "$NEED_ROOT" = "1" ]; then
        say "macOS will ask for your Mac login password (port 53 and the firewall need it). Nothing shows while you type."
        sudo "$PY" scopsDNS.py "$@" 2>&1 | keep_log
    else
        "$PY" scopsDNS.py "$@" 2>&1 | keep_log
    fi
    line
    say "Saved a copy for Discord: scops-mac-report.txt (menu option 6 copies it)."
}

# Ctrl+C stops the running check or DNS and comes back to this menu instead of closing the window.
trap 'true' INT

if ! find_python; then
    line
    say "Python 3 is needed and was not found."
    say "A box may have appeared asking to install the 'command line developer tools': click Install,"
    say "wait for it to finish (a few minutes), then double-click SCOPS Mac.command again."
    say "(Or install Python 3 from python.org.)"
    line
    /usr/bin/xcode-select --install >/dev/null 2>&1
    read -r -p "Press Return to close." _
    exit 1
fi
if [ ! -f scopsDNS.py ]; then
    say "scopsDNS.py is missing. Keep it in the same folder as SCOPS Mac.command."
    read -r -p "Press Return to close." _
    exit 1
fi

while true; do
    clear
    MYIP="$(mac_ip)"
    line
    say " SCOPS for Mac - Splinter Cell Chaos Theory PS2 online"
    say " This Mac: ${MYIP:-no network address found}      SCOPS server: $SERVER"
    line
    say " Playing (everyone):"
    say "   1) Check my DNS          - tells you which DNS to type into the PS2 / PCSX2"
    say "   2) Run local DNS         - only if check 1 says your DNS is redirected/hijacked"
    say ""
    say " Hosting (optional):"
    say "   3) Host - real PS2 or PCSX2 PCAP Bridged: local DNS + open router ports (UPnP) to the console"
    say "   4) Host - PCSX2 Sockets mode on THIS Mac: local DNS + open router ports (UPnP) to this Mac"
    say ""
    say "   5) Remove the firewall rules scopsDNS added"
    say "   6) Copy the last report (paste it in Discord with Cmd+V)"
    say "   7) Find my PS2 on this network (real PS2 or PCSX2 PCAP Bridged; start the game's online menu first)"
    say "   q) Quit"
    line
    read -r -p " Choose: " CHOICE
    clear
    NEED_ROOT=0
    case "$CHOICE" in
        1)
            say "Checking DNS (about 10 seconds)..."
            run_logged --check-only --server "$SERVER"
            say ""
            say "What to do with the result:"
            say " - No hijack / no changes needed: in the PS2 network settings set DNS to manual and put"
            say "   $SERVER in BOTH DNS boxes (primary and secondary). You can close this window."
            say " - Hijacked / redirected: use option 2 instead, and put this Mac's address (${MYIP:-shown above})"
            say "   in both DNS boxes. Keep that window open while you play."
            ;;
        2)
            NEED_ROOT=1
            say "Local DNS: put ${MYIP:-the Mac address shown at the top} in BOTH DNS boxes on the PS2 / PCSX2."
            say "Keep this window open while you play. Press Ctrl+C to stop."
            run_logged --no-upnp --open-firewall --server "$SERVER"
            ;;
        3)
            GUESS=""
            if valid_ip "$MYIP"; then
                say "Looking for your PS2 first (it must be at the game's online menu)..."
                FOUND="$("$PY" scopsDNS.py --find-ps2 --ip "$MYIP" 2>&1)"
                printf '%s\n' "$FOUND"
                GUESS="$(printf '%s\n' "$FOUND" | sed -n 's/.*PS2 FOUND: \([0-9.]*\).*/\1/p' | head -n 1)"
                if [ -z "$GUESS" ] && [ "$(printf '%s\n' "$FOUND" | grep -c 'SONY DEVICE:')" = "1" ]; then
                    GUESS="$(printf '%s\n' "$FOUND" | sed -n 's/.*SONY DEVICE: \([0-9.]*\).*/\1/p')"
                fi
            fi
            if [ -n "$GUESS" ]; then
                read -r -p "Press Return to use $GUESS, or type the PS2 address: " PS2
                [ -z "$PS2" ] && PS2="$GUESS"
            else
                read -r -p "PS2 / PCAP Bridged address (e.g. 192.168.1.240, see the console's network settings): " PS2
            fi
            if ! valid_ip "$PS2"; then say "That is not an address like 192.168.1.240."; else
                NEED_ROOT=1
                say "Opening UDP 3658 and 10070-10080 to $PS2 through UPnP, and running local DNS."
                say "Put ${MYIP:-the Mac address shown at the top} in both DNS boxes (or $SERVER if check 1 said no changes needed)."
                say "Keep this window open while you host; Ctrl+C closes the ports again."
                run_logged --upnp "$PS2" --open-firewall --server "$SERVER"
            fi
            ;;
        4)
            if ! valid_ip "$MYIP"; then say "Could not find this Mac's address. Is it connected to the network?"; else
                NEED_ROOT=1
                say "PCSX2 Sockets: the game arrives on this Mac ($MYIP)."
                say "Opening UDP 3658 and 10070-10080 to $MYIP through UPnP, and running local DNS."
                say "If macOS asks whether PCSX2 may accept incoming network connections, click Allow."
                say "Keep this window open while you host; Ctrl+C closes the ports again."
                run_logged --upnp "$MYIP" --pcsx2-ports --open-firewall --server "$SERVER"
            fi
            ;;
        5)
            NEED_ROOT=1
            run_logged --close-firewall
            ;;
        6)
            if [ -f "$LOG" ]; then
                pbcopy < "$LOG" && say "Copied. Paste it in Discord with Cmd+V."
            else
                say "No report yet. Run option 1 first."
            fi
            ;;
        7)
            if ! valid_ip "$MYIP"; then say "Could not find this Mac's address. Is it connected to the network?"; else
                run_logged --find-ps2 --ip "$MYIP"
                say "Use the PS2 FOUND address for hosting (option 3). It can change after the router restarts."
            fi
            ;;
        q|Q) exit 0 ;;
        *) say "Type a number from 1 to 7, or q." ;;
    esac
    say ""
    read -r -p "Press Return for the menu." _
done
