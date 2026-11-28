#!/usr/bin/env python3
"""Reset a player's password by hand (2026-11-25), for when they've forgotten it and can't log in to change it
themselves. Passwords are hashed (tools/ubigs_userdb.py) so there is no way to look up or display the old one -
this only sets a new one. It does not touch last_login_ip/last_login_ts, dp or created_ts, so the player just
logs in with the new password next time; nothing else about the account changes.

Interactive: asks for the username, shows what's on file for it, asks for the new password twice (hidden,
like a normal password prompt) and confirms before writing. Does not create a new account - if the username
isn't found, it says so and stops.

Usage:
  python tools/reset_player_password.py                 state/users.json
  python tools/reset_player_password.py --user-db other_users.json
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from ubigs_userdb import UserDB  # noqa: E402


def _when(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "unknown"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user-db", default=str(ROOT / "state" / "users.json"), help="path to users.json")
    a = ap.parse_args(argv)
    db = UserDB(a.user_db)
    db.load()

    username = input("Username to reset: ").strip()
    if not username:
        print("No username entered.")
        return 1
    u = db.get_user(username)
    if u is None:
        print(f"No account named {username!r} found in {a.user_db}.")
        return 1

    print(f"\nFound {username!r}:")
    print(f"  game: {u.get('dp', 'unknown')}")
    print(f"  created: {_when(u.get('created_ts'))}" if u.get("created_ts") else "  created: unknown")
    if u.get("last_login_ip"):
        print(f"  last password login: {u['last_login_ip']} at {_when(u.get('last_login_ts'))}")
    else:
        print("  last password login: never recorded")

    pw1 = getpass.getpass("\nNew password (hidden): ")
    if not pw1.strip():
        print("Empty password, nothing done.")
        return 1
    pw2 = getpass.getpass("Type it again to confirm: ")
    if pw1 != pw2:
        print("Those didn't match. Nothing was changed.")
        return 1

    ok = input(f"\nSet a new password for {username!r} now? (Y/N) ").strip().lower()
    if ok != "y":
        print("Cancelled. Nothing was changed.")
        return 0

    db.upsert_user(username=username, password=pw1)   # dp/created_ts untouched (only passed fields change)
    db.save()
    print(f"\nDone. {username!r} can log in with the new password - nothing else about the account changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
