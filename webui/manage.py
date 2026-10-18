#!/usr/bin/env python3
"""Configuration utility for the IKEv2 Web UI."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from webui.security import AuthConfig, hash_password, locked_config, write_existing_config


def read_password_hash(args: argparse.Namespace) -> str:
    if args.password_stdin:
        password = sys.stdin.read(4097)
        if len(password) > 4096:
            raise SystemExit("Password must contain at most 4096 characters.")
    else:
        password = getpass.getpass("Web UI password (minimum 12 characters): ")
        confirmation = getpass.getpass("Confirm Web UI password: ")
        if password != confirmation:
            raise SystemExit("Passwords do not match.")
    if "\n" in password or "\r" in password:
        raise SystemExit("Password must not contain newline characters. Supply stdin without a trailing newline.")
    try:
        return hash_password(password)
    except ValueError as error:
        raise SystemExit(str(error)) from error


def create_config(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {output}")
    password_hash = read_password_hash(args)
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


def reset_password(args: argparse.Namespace) -> None:
    path = args.config.resolve()
    try:
        AuthConfig.from_file(path)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    password_hash = read_password_hash(args)
    with locked_config(path):
        AuthConfig.from_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if args.username is not None:
            if not 1 <= len(args.username) <= 64:
                raise SystemExit("Username must contain between 1 and 64 characters.")
            payload["username"] = args.username
        payload["password_hash"] = password_hash
        payload.pop("totp_secret", None)
        payload.pop("totp_last_counter", None)
        write_existing_config(path, payload)
    print("Web UI credentials updated; two-factor authentication disabled. Restart ikev2-web.service to apply and invalidate existing sessions.")


def configure_cookie(args: argparse.Namespace) -> None:
    path = args.config.resolve()
    try:
        AuthConfig.from_file(path)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    with locked_config(path):
        AuthConfig.from_file(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["secure_cookie"] = not args.allow_http
        write_existing_config(path, payload)
    print("Cookie security updated; credentials preserved. Restart ikev2-web.service to apply.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage IKEv2 Web UI configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-config", help="create a password-protected config")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--username", default="admin")
    create.add_argument(
        "--password-stdin", action="store_true",
        help="read the exact initial password from stdin without prompting",
    )
    create.add_argument("--session-ttl", type=int, default=3600, choices=range(300, 86401))
    create.add_argument(
        "--allow-http",
        action="store_true",
        help="disable the Secure cookie flag for loopback-only development",
    )
    reset = subparsers.add_parser("reset-password", help="replace credentials in an existing config")
    reset.add_argument("--config", type=Path, required=True)
    reset.add_argument("--username", help="optionally replace the existing username")
    reset.add_argument("--password-stdin", action="store_true", help="read the exact new password from stdin")
    cookie = subparsers.add_parser("configure-cookie", help="change cookie security without changing credentials")
    cookie.add_argument("--config", type=Path, required=True)
    cookie.add_argument("--allow-http", action="store_true", help="disable Secure cookies for loopback access")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "create-config":
        create_config(args)
    elif args.command == "reset-password":
        reset_password(args)
    elif args.command == "configure-cookie":
        configure_cookie(args)


if __name__ == "__main__":
    main()
