#!/usr/bin/env python3
"""Print a bcrypt hash for a password, for pasting into HYROX_USERS."""

import getpass

import bcrypt


def main() -> None:
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Repeat: "):
        raise SystemExit("passwords do not match")
    print(bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode())


if __name__ == "__main__":
    main()
