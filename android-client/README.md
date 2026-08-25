# Native Android IKEv2 Client v1.1.0

This is the Android 11+ (API 30+) client for the repository's strongSwan
configuration. It uses Kotlin, Jetpack Compose, Material 3, and Android's native
VPN APIs.

```text
Android App
    ↓
VpnManager / Ikev2VpnProfile
    ↓
Android platform IKEv2/IPsec stack
    ↓
strongSwan server
```

The app provisions the profile; Android performs IKEv2/IPsec processing. The
app does not implement IKEv2, intercept VPN traffic, use `VpnService`, or embed
a native VPN engine.

## Supported v1.1 scope

Supported: Android 11+/API 30+ devices with the platform
`android.software.ipsec_tunnels` feature, hostname or IPv4 server addresses,
IKEv2 with EAP-MSCHAPv2, a private CA, portable `.ikev` schema v1 import, one
IPv4 full-tunnel profile, provisioning, VPN consent, connect/disconnect, basic
status, and sanitized diagnostics.

Not supported: IPv6 literals, SOCKS5 or split-tunnel Proxy Mode, QR
provisioning, multiple profiles, per-app VPN, transparent proxy, custom DNS or
routing, a custom `VpnService`, Quick Settings, or auto-connect UI.

## Portable `.ikev` onboarding

Android v1.1 supports `.ikev` schema version 1 only. The file contains the
server, Remote ID, username, public CA certificate, CA SHA-256 fingerprint, and
informational server/proxy metadata. **`.ikev` files never contain VPN passwords
or private keys.** The original JSON and Base64 certificate text are read once
and are not persisted.

1. Export `username.ikev` on the server.
2. Copy or share it to Android.
3. Open the IKEv2 Android client.
4. Tap **Import .ikev Profile**.
5. Select the file.
6. Review the server, username, and CA fingerprint.
7. Enter the VPN password.
8. Tap **Save / Provision VPN**.
9. Approve Android VPN consent if requested.
10. Connect.

Import validates the frozen format and version, EAP-MSCHAPv2 authentication,
Full Tunnel mode, profile/server/username fields, embedded DER CA validity and
CA Basic Constraints, and the CA SHA-256 fingerprint. It populates the existing
setup screen but does not provision or replace the current Android VPN profile
until **Save / Provision VPN** is pressed.

Android's current simple `Ikev2VpnProfile.Builder` path cannot represent an
independent server Remote ID, so imported `remote_id` must equal `server`.
Profiles that differ are rejected.

Android v1.1 remains Full Tunnel only. Validated SOCKS5 Proxy Mode metadata is
displayed for review but is not configured; the app does not create proxy
sockets, split routes, or a custom `VpnService`.

## Manual setup inputs

Copy only the following values from the server installation:

```text
VPN Server:     <certificate-matching hostname or IPv4 address>
Username:       <configured EAP user>
Password:       <configured EAP password>
CA Certificate: ca-cert.cer
```

Do not copy the CA private key, server private key, or any server certificate
private key to Android. The public `ca-cert.cer` is sufficient. Portable import
embeds this same public certificate, so no separate CA file is needed when using
`username.ikev`.

## Identity and certificate behavior

The app currently builds `Ikev2VpnProfile` with the configured server address
as the gateway/remote IKE identity and the username as the local IKE identity.
It also supplies that username for EAP-MSCHAPv2 authentication. This coupling
matches the tested strongSwan configuration, whose client identity policy is
permissive; an EAP username and local IKE identity are not universally the same
concept. **TODO:** add a separate local IKE identity field if a future server
policy requires a distinct IDi.

The selected DER or PEM file must contain exactly one currently valid X.509
certificate. The app displays its subject, issuer, CA warning, and SHA-256
fingerprint, stores canonical DER privately, and passes it directly as
`serverRootCa`. It does not install a global CA or bypass server identity checks.

The password is required for each provisioning or reprovisioning operation. It
exists transiently in a non-saveable setup field but is not copied into the
ViewModel/StateFlow or saved in DataStore, files, backups, logs, or diagnostics.

## Manual configure and connect

1. Enter a display name, the exact server hostname/IPv4 address, EAP username,
   and password.
2. Import the server installer's `ca-cert.cer` and verify its subject, issuer,
   and SHA-256 fingerprint against a trusted copy.
3. Select **Save / Provision VPN** and accept Android's VPN consent prompt.
4. Select **CONNECT** and wait for platform-confirmed Connected status. Use
   **Diagnostics** for sanitized state, identity, CA, session, and error details.
5. Select **DISCONNECT** and verify ordinary connectivity returns.

## Build and project layout

Install JDK 17 and Android SDK 36, then run from `android-client/`:

```bash
./gradlew testDebugUnitTest
./gradlew lintDebug
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Production code is under `app/src/main/java/com/saeed/ikev2vpn/`:

- `certificate/`: bounded SAF import, parsing, and SHA-256 fingerprints
- `data/`: one-profile DataStore metadata and private canonical CA storage
- `profile/`: bounded `.ikev` reads and strict portable schema validation
- `vpn/`: native provisioning, SDK compatibility, state, and events
- `ui/`: Compose screens and ViewModel/StateFlow state
- `validation/`: profile and server-address validation
- `app/src/test/`: hermetic JUnit certificate, validation, reducer, and state tests

## Local release signing

Release APKs never fall back to the debug key. Create the production key once
and keep it in secured, backed-up storage outside the repository:

```bash
keytool -genkeypair -v \
  -keystore /secure/path/ikev2-android-release.jks \
  -alias ikev2-android \
  -keyalg RSA -keysize 4096 -validity 10000
```

Set the signing inputs without committing them. Prompting avoids putting
passwords in shell history:

```bash
export ANDROID_KEYSTORE_PATH=/secure/path/ikev2-android-release.jks
export ANDROID_KEY_ALIAS=ikev2-android
read -rsp "Keystore password: " ANDROID_KEYSTORE_PASSWORD; export ANDROID_KEYSTORE_PASSWORD; printf '\n'
read -rsp "Key password: " ANDROID_KEY_PASSWORD; export ANDROID_KEY_PASSWORD; printf '\n'
```

All four variables are required for an explicit release task:
`ANDROID_KEYSTORE_PATH`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, and
`ANDROID_KEY_PASSWORD`. Missing values cause release tasks to fail without
printing secrets; debug builds, tests, and lint do not require them.

**Retain the same production signing key for every future update.** Losing it
prevents users from installing upgrades over the existing application. Never
commit a keystore or password.

## Repeatable android-v1.1.0 release

Confirm `versionCode = 2` and `versionName = "1.1.0"`, complete
[RELEASE_TESTING.md](RELEASE_TESTING.md), and ensure the working tree contains
only intended release changes. With signing variables set:

```bash
./gradlew --no-daemon --no-configuration-cache clean testDebugUnitTest lintDebug assembleRelease
mkdir -p dist
cp app/build/outputs/apk/release/app-release.apk dist/ikev2-android-v1.1.0.apk
apksigner verify --verbose --print-certs dist/ikev2-android-v1.1.0.apk
(cd dist && sha256sum ikev2-android-v1.1.0.apk > ikev2-android-v1.1.0.apk.sha256)
(cd dist && sha256sum -c ikev2-android-v1.1.0.apk.sha256)
unset ANDROID_KEYSTORE_PATH ANDROID_KEYSTORE_PASSWORD ANDROID_KEY_ALIAS ANDROID_KEY_PASSWORD
```

The v1.1 release build intentionally leaves R8/minification disabled to reduce
first-release risk.

Always keep `--no-daemon --no-configuration-cache` on signed-release commands
so the signing process exits after the build and Gradle does not persist values
supplied to the Android plugin during configuration. `apksigner` is supplied by
Android SDK Build Tools; use its full SDK path if it is not on `PATH`, and retain
the reported signer-certificate SHA-256 digest with the release record.

Publish both files only after validation:

```text
dist/ikev2-android-v1.1.0.apk
dist/ikev2-android-v1.1.0.apk.sha256
```

The human release owner may then create the Android-specific tag
`android-v1.1.0`; no build command creates, pushes, or publishes it.

## Future signed-release CI

The normal CI workflow intentionally needs no signing secrets and does not
publish releases. A future protected release workflow should use these GitHub
Actions secrets:

- `ANDROID_KEYSTORE_BASE64`
- `ANDROID_KEYSTORE_PASSWORD`
- `ANDROID_KEY_ALIAS`
- `ANDROID_KEY_PASSWORD`

Decode the keystore only into a temporary runner path, expose that path as
`ANDROID_KEYSTORE_PATH`, avoid command tracing, and delete the file after the
build. Restrict the secrets to protected release jobs and never upload the
keystore itself. The base64 value is sensitive key material, not encryption.

## Platform behavior and limitations

API 33+ uses `startProvisionedVpnProfileSession()`, `VpnProfileState`, and
protected VPN-manager events. API 30–32 isolates the deprecated start call and
reports Connected only after Android exposes an app-owned VPN network. Vendor
implementations may report transitions or failure details slowly.

See [RELEASE_TESTING.md](RELEASE_TESTING.md) for the mandatory API 30–32 and API
33+ real-device gates. Automated JVM tests cannot establish an IKEv2 tunnel.

The repository is licensed under the [MIT License](../LICENSE).
