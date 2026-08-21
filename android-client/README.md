# Native Android IKEv2 Client — Phase 1

This app provisions one strongSwan-compatible IKEv2 profile through Android's platform VPN stack. It uses Kotlin, Jetpack Compose, Material 3, `VpnManager`, and `Ikev2VpnProfile`; it does not contain a `VpnService`, custom VPN engine, VPN-related native library, or custom traffic path.

## Requirements and build

- Android 11 (API 30) or newer with `android.software.ipsec_tunnels`
- JDK 17 and Android SDK 36

From this directory:

```bash
./gradlew testDebugUnitTest
./gradlew assembleDebug
./gradlew lintDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The Gradle wrapper downloads build dependencies on its first run.

## Project layout

- `app/src/main/.../certificate/`: bounded SAF import, X.509 parsing, and SHA-256 fingerprints
- `data/`: one-profile DataStore metadata and canonical DER CA storage
- `vpn/`: platform provisioning, API compatibility, state reduction, and API 33+ events
- `ui/`: Compose setup, main, and diagnostics screens backed by one ViewModel/StateFlow
- `validation/`: profile and server-address validation
- `app/src/test/`: hermetic certificate, validation, and state tests

## Platform and security behavior

The configured server address is both the gateway and Android's remote IKE identity. The username is the local IKE identity and the EAP-MSCHAPv2 username. The imported CA is passed directly as `serverRootCa`; it is never installed in Android's global trust store.

Certificate import accepts DER or PEM content selected through the document picker. The app requires exactly one currently valid X.509 certificate, stores canonical DER in private storage, verifies its stored fingerprint on load, and warns if the certificate lacks the CA basic constraint.

The password lives in a non-saveable local Compose field and is passed directly through one synchronous provisioning call. It is cleared before provisioning and whenever the app leaves the foreground, and is never retained by the ViewModel, StateFlow, DataStore, diagnostics, logs, or backups. Reprovisioning therefore requires entering it again. Android's provisioned profile is the intended credential boundary.

On API 33+, connection starts with `startProvisionedVpnProfileSession()` and state comes from `VpnProfileState`, supplemented by protected VPN-manager events. On API 30–32, the deprecated start call is isolated in the controller and connection is confirmed only when Android exposes a VPN network owned by this app. A successful start request alone remains `Connecting`.

## Configure and connect

1. Open the app and enter a display name, the exact server hostname/IP, username, and password.
2. Select **Import CA Certificate** and choose the server installer's `ca-cert.cer` (DER or PEM `.cer`/`.crt` content is supported).
3. Verify the displayed subject, issuer, and SHA-256 fingerprint.
4. Select **Save / Provision VPN**, then accept Android's system VPN consent dialog if shown.
5. Select **CONNECT**. Use **Diagnostics** to inspect platform support, state evidence, identity, CA, session, and sanitized errors.
6. Select **DISCONNECT** to restore normal connectivity.

## Real-device acceptance

Automated builds cannot exercise Android's IKE stack. Test on at least one API 30–32 device and one API 33+ device against the existing server:

1. Install the debug APK and record the device's current public IPv4 address.
2. Enter valid server details, import the installer's CA, and compare the displayed SHA-256 fingerprint with `openssl x509 -inform DER -in ca-cert.cer -noout -fingerprint -sha256` on a trusted machine.
3. Deny the first consent prompt; confirm the app remains unconfigured and usable. Provision again and approve it.
4. Open Diagnostics and verify the server/remote identity, local identity, CA subject, issuer, and fingerprint.
5. Select **CONNECT** and wait for **Connected** with confirmed Android evidence.
6. Confirm Android assigns a VPN address, run `sudo ./ikev2-strongswan-ubuntu-v6.1.1.sh status` on the server to inspect the established session, and verify the server-supplied DNS resolves a fresh hostname.
7. Run a public-IP check and confirm it shows the VPN server's public IPv4 address.
8. Select **DISCONNECT**; confirm ordinary DNS, Internet access, and the original public IP return.
9. Repeat separately with a wrong password, unrelated CA, mismatched server identity, and unreachable server. Each must fail without showing a false confirmed connection, exposing credentials, or breaking normal networking.
10. Rotate during setup and after provisioning, then relaunch. Non-secret profile data must remain intact while the password must be cleared.

## Known limitations and Phase 2

Phase 1 supports one full-tunnel profile only. API 30–32 state and failure detail are necessarily best-effort, and vendor implementations may report transitions slowly. API 33+ event diagnostics cover common IKE/network failures but are not a packet-level monitor. There is no profile deletion UI, automatic connection, custom DNS/routing, traffic statistics, or server administration.

Possible Phase 2 work includes QR provisioning, restricted-selector Proxy Mode, richer API 33+ diagnostics, multiple profiles, and Quick Settings integration.
