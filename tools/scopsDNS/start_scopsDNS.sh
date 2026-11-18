#!/bin/sh
# macOS / Linux: port 53 and firewall changes need root.
#   ./start_scopsDNS.sh                         start
#   ./start_scopsDNS.sh --open-firewall         also allow DNS through the firewall (add --pcsx2-ports for PCSX2 here)
#   ./start_scopsDNS.sh --close-firewall        remove those rules
cd "$(dirname "$0")"
exec sudo python3 scopsDNS.py "$@"
