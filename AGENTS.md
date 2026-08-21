# Repository Guidelines

## Project Structure & Module Organization

The repository contains three top-level platform scripts and one Android project. `ikev2-strongswan-ubuntu-v6.1.1.sh` manages the Ubuntu StrongSwan server; `ikev2-linux-client-v1.5.sh` and `ikev2-windows-client-v6.ps1` manage desktop clients. `android-client/` is a single-module Kotlin/Compose application. Its production code is under `android-client/app/src/main/java/com/saeed/ikev2vpn/`, grouped into `certificate/`, `data/`, `ui/`, `validation/`, and `vpn/`. JVM tests mirror those packages under `app/src/test/`. Keep generated certificates and credentials out of source control.

## Build, Test, and Development Commands

Run Android commands from `android-client/` with JDK 17 and Android SDK 36 configured:

```bash
./gradlew testDebugUnitTest   # Run hermetic JVM tests
./gradlew assembleDebug       # Build app/build/outputs/apk/debug/app-debug.apk
./gradlew lintDebug           # Run Android lint
```

From the repository root, run `bash -n ikev2-strongswan-ubuntu-v6.1.1.sh ikev2-linux-client-v1.5.sh` and, when installed, ShellCheck on both files. Use disposable VMs or devices for privileged VPN integration tests.

## Coding Style & Naming Conventions

Use four-space Kotlin indentation, immutable state, `PascalCase` types/composables, `camelCase` functions and properties, and `UPPER_SNAKE_CASE` constants. Keep Android framework calls out of composables and SDK branches inside the VPN compatibility layer. Preserve Bash strict mode, quoted expansions, `snake_case` functions, and each script's existing indentation. PowerShell functions use `Verb-Noun` names and four spaces.

## Testing Guidelines

Use JUnit 4 for pure certificate, validation, and state-reducer logic. Name tests as readable behaviors, for example ``fun `valid PEM and DER have the same fingerprint`()``. Do not mock a complete IKEv2 tunnel. Manually verify provisioning, consent denial, connect/disconnect, DNS, routing, bad credentials, bad CA, rotation, and relaunch on supported real devices.

## Commit & Pull Request Guidelines

History is too small to establish a convention. Use a concise imperative subject such as `Add Android consent handling`, with one logical change per commit. PRs must describe affected platforms, security and rollback impact, linked issues, test commands and OS/API versions, plus sanitized screenshots for UI changes. Update the relevant README whenever behavior or prompts change.

## Security & Configuration

Never commit passwords, private keys, generated credentials, production certificates, state backups, or unsanitized logs. Call out changes involving CA trust, VPN consent, root/admin actions, firewall/NAT, routing, DNS, or uninstall restoration.
