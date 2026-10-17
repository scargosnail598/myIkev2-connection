#!/usr/bin/env python3
"""Configuration utility for the IKEv2 Web UI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path

from webui.security import hash_password


def create_config(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {output}")
    password = getpass.getpass("Web UI password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm Web UI password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    try:
        password_hash = hash_password(password)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    payload = {
        "username": args.username,
        "password_hash": password_hash,
        "secure_cookie": not args.allow_http,
        "session_ttl_seconds": args.session_ttl,
    }
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(output, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        json.dump(payload, config_file, indent=2)
        config_file.write("\n")
    print(f"Created {output} with mode 0600.")
    if args.allow_http:
        print("Warning: Secure cookies are disabled. Use this config only for loopback HTTP testing.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage IKEv2 Web UI configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-config", help="create a password-protected config")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--username", default="admin")
    create.add_argument("--session-ttl", type=int, default=3600, choices=range(300, 86401))
    create.add_argument(
        "--allow-http",
        action="store_true",
        help="disable the Secure cookie flag for loopback-only development",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "create-config":
        create_config(args)


if __name__ == "__main__":
    main()
