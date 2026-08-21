# Android v1.0.0 Release Testing

Complete this checklist against the release candidate before creating
`android-v1.0.0`. Run every check on both device rows. Prefer different OEM
families when practical; one family is acceptable if no alternative is
available. Record `PASS`, `FAIL`, or `BLOCKED`, and never infer a device result
from JVM tests.

## Device record matrix

| Device | Android version | API level | Manufacturer | Build type | Result | Notes |
|---|---|---:|---|---|---|---|
| Device A |  | 30–32 |  | Signed release |  |  |
| Device B |  | 33+ |  | Signed release |  |  |

Record the APK SHA-256 and test date in Notes. A stable release requires both
API rows to pass. Only the preference for different OEM families may receive a
written, reviewed exception.

## Test setup

- Use a disposable EAP account and the server's public `ca-cert.cer`.
- Record the device's original public IPv4, DNS behavior, and active network.
- Verify the APK checksum before installation and capture only sanitized logs.
- Compare the imported CA fingerprint with a value obtained on a trusted host:

  ```bash
  openssl x509 -inform DER -in ca-cert.cer -noout -fingerprint -sha256
  ```

## Positive acceptance checks

- [ ] Install the signed release APK without relying on a debug installation.
- [ ] Enter the certificate-matching hostname or IPv4 address, EAP username,
      password, and import the correct CA.
- [ ] Confirm the displayed subject, issuer, and SHA-256 fingerprint; accept the
      Android VPN consent prompt and provision successfully.
- [ ] Select **CONNECT**; confirm the app reports Connected only with platform
      evidence and strongSwan reports an established IKEv2 session.
- [ ] Confirm a virtual IP is assigned, DNS resolves a fresh hostname, all IPv4
      Internet traffic works, and the public IPv4 matches the VPN server.
- [ ] Select **DISCONNECT**; confirm the tunnel closes and the original network,
      DNS, Internet access, and public IPv4 return.
- [ ] Connect again after disconnect and repeat the connection/disconnection
      checks.
- [ ] Rotate during setup and while provisioned; verify fields/profile remain
      coherent, no operation duplicates, and the password is cleared.
- [ ] Relaunch after provisioning; verify non-secret profile data remains,
      password is blank, and connect/disconnect still work.

## Negative acceptance checks

Perform each case independently and restore valid inputs afterward:

- [ ] Incorrect password: authentication fails without a false Connected state.
- [ ] Incorrect/unrelated CA: server authentication fails safely.
- [ ] Wrong server identity (for example, a DNS alias absent from the server
      certificate SAN): certificate identity mismatch fails safely.
- [ ] Unreachable server: timeout/error remains conservative and can be stopped.
- [ ] Invalid hostname: validation rejects it before provisioning.
- [ ] VPN permission denied: app remains usable and does not retain a provisioned
      state.
- [ ] Empty or malformed certificate: import is rejected without replacing the
      previously committed profile/CA.
- [ ] Relaunch during an unconnected provisioned state: no crash or false state.
- [ ] Screen rotation during setup/consent/connection: no crash, duplicate
      provisioning, or password retention.

For every failure, verify the app can recover using valid inputs, ordinary
networking remains usable, and no password or complete credentials appear in
UI diagnostics, captured logs, files, or screenshots.

## Release sign-off

| Gate | Result | Evidence / Notes |
|---|---|---|
| API 30–32 full checklist |  |  |
| API 33+ full checklist |  |  |
| `testDebugUnitTest` |  |  |
| `lintDebug` |  |  |
| `assembleDebug` |  |  |
| Signed `assembleRelease` |  |  |
| APK signature verified |  |  |
| APK checksum verified |  |  |
| Signing key backup verified |  |  |

Do not mark the stable release ready while any required gate is failed or
unexplained. Record device-specific quirks and attach only sanitized evidence.
