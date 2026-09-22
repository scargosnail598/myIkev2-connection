# Repository Guidelines

## Project Structure & Module Organization

The repository contains top-level platform scripts: `ikev2-strongswan-ubuntu.sh` manages the Ubuntu StrongSwan server, while the Linux and Windows scripts manage desktop clients. Keep generated certificates and credentials out of source control.

## Build, Test, and Development Commands

From the repository root, run `bash -n ikev2-strongswan-ubuntu.sh ikev2-linux-client-v1.6.sh` and, when installed, ShellCheck on both files. Use disposable VMs for privileged VPN integration tests.

## Coding Style & Naming Conventions

Preserve Bash strict mode, quoted expansions, `snake_case` functions, and each script's existing indentation. PowerShell functions use `Verb-Noun` names and four spaces.

## Testing Guidelines

Manually verify provisioning, connect/disconnect, DNS, routing, bad credentials, bad CA, certificate rotation, and relaunch on supported systems.

## Commit & Pull Request Guidelines

History is too small to establish a convention. Use a concise imperative subject with one logical change per commit. PRs must describe affected platforms, security and rollback impact, linked issues, test commands and OS versions. Update the relevant README whenever behavior or prompts change.

## Security & Configuration

Never commit passwords, private keys, generated credentials, production certificates, state backups, or unsanitized logs. Call out changes involving CA trust, VPN consent, root/admin actions, firewall/NAT, routing, DNS, or uninstall restoration.
