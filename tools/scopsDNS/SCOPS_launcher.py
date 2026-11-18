"""Double-click launcher for the SCOPS window. No console.

Builds to SCOPS.exe. Starts the WinForms window next to this file.
The window then starts scopsDNS.exe or scopsDNS.py.
"""
import os
import subprocess
import sys


def here() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    folder = here()
    ps1 = os.path.join(folder, "SCOPS_UPnP_Check.ps1")
    if not os.path.isfile(ps1):
        return 1
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-STA",
            "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden",
            "-File", ps1,
        ],
        cwd=folder,
        close_fds=True,
        creationflags=flags,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
