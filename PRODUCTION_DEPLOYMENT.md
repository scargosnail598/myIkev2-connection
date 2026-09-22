# Production Deployment with GitHub Actions

This guide explains how to prepare an Ubuntu StrongSwan server and configure GitHub Actions to deploy the server installer safely.

The examples use:

- Server IP: `155.117.13.45`
- SSH port: `9011`
- Installer: `ikev2-strongswan-ubuntu.sh`

> Port `9011` is treated as the SSH port. StrongSwan still requires UDP ports `500` and `4500` for IKEv2.

## 1. Prepare the Server

Use Ubuntu Server `22.04` or `24.04` with a public IPv4 address.

Configure the provider firewall or security group with:

| Protocol | Port | Source | Purpose |
|---|---:|---|---|
| TCP | `9011` | Your administration IPs | SSH |
| UDP | `500` | `0.0.0.0/0` | IKEv2 |
| UDP | `4500` | `0.0.0.0/0` | IKEv2 NAT-T |

Do not expose the SOCKS5 proxy port publicly. The installer keeps Proxy Mode VPN-only.

Verify that SSH is listening on the expected port before closing the current session:

```bash
sudo ss -lntp | grep ':9011'
```

The first installation should be performed manually because it asks for the server identity, VPN subnet, DNS servers, CA name, VPN users, and optional Proxy Mode settings.

Copy the installer to the server and run it:

```bash
scp -P 9011 ikev2-strongswan-ubuntu.sh admin@155.117.13.45:/tmp/
ssh -p 9011 admin@155.117.13.45

chmod +x /tmp/ikev2-strongswan-ubuntu.sh
sudo /tmp/ikev2-strongswan-ubuntu.sh install
```

Use `155.117.13.45` as the server address or identity unless you have a DNS hostname that clients will use instead.

After installation, verify the server:

```bash
sudo /tmp/ikev2-strongswan-ubuntu.sh status
sudo /tmp/ikev2-strongswan-ubuntu.sh diagnostics
sudo ss -lunp | grep -E ':(500|4500)\\b'
```

## 2. Create a Dedicated Deployment Key

Generate a new Ed25519 key on your administration workstation. Do not reuse your personal SSH key.

```bash
ssh-keygen -t ed25519 -C "github-actions-production-deploy" \\
  -f ~/.ssh/ikev2_github_actions_production
```

This creates:

- Private key: `~/.ssh/ikev2_github_actions_production`
- Public key: `~/.ssh/ikev2_github_actions_production.pub`

Install only the public key on the server:

```bash
ssh-copy-id -i ~/.ssh/ikev2_github_actions_production.pub \\
  -p 9011 admin@155.117.13.45
```

If `ssh-copy-id` is unavailable, append the public key manually while logged in through the existing administrative session:

```bash
install -d -m 700 ~/.ssh
cat ~/.ssh/ikev2_github_actions_production.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Test the key before adding it to GitHub:

```bash
ssh -i ~/.ssh/ikev2_github_actions_production \\
  -p 9011 admin@155.117.13.45 'id && sudo -n true'
```

The command must succeed without asking for an SSH password. The deployment account needs passwordless `sudo` for the specific deployment commands, or a carefully restricted sudo policy. Do not store a sudo password in GitHub Secrets.

## 3. Create a Verified `known_hosts` Value

GitHub Actions must verify the server host key. Do not disable host-key checking.

First inspect the key from a trusted administration connection:

```bash
ssh-keyscan -p 9011 155.117.13.45
```

Compare the displayed fingerprint with the fingerprint shown by the server owner or cloud console. After verification, save the complete `ssh-keyscan` output as the `PRODUCTION_KNOWN_HOSTS` value.

If the server is rebuilt and its host key changes, update this GitHub secret deliberately. A changed host key can indicate either a legitimate rebuild or a man-in-the-middle attack.

## 4. Create a GitHub Environment

In the repository:

1. Open **Settings**.
2. Open **Environments**.
3. Select **New environment**.
4. Name it `production`.
5. Add required reviewers for production deployment.
6. Optionally restrict deployment branches or tags to the release branch/tag pattern.

Use an environment instead of repository-wide secrets so production deployment can require approval and has a separate audit trail.

## 5. Add Environment Variables

Under **Settings > Environments > production > Variables**, add:

| Name | Value | Secret? |
|---|---|---|
| `PRODUCTION_HOST` | `155.117.13.45` | No |
| `PRODUCTION_PORT` | `9011` | No |
| `PRODUCTION_USER` | Your dedicated SSH user, for example `deploy` | No |
| `PRODUCTION_INSTALL_DIR` | `/opt/ikev2` | No |

The IP address, SSH port, and username are configuration values, not credentials.

## 6. Add Environment Secrets

Under **Settings > Environments > production > Secrets**, add:

| Name | Value |
|---|---|
| `PRODUCTION_SSH_PRIVATE_KEY` | Complete contents of the Ed25519 private key file |
| `PRODUCTION_KNOWN_HOSTS` | Verified `ssh-keyscan` output for port `9011` |

The private key must include its complete header and footer:

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

Never add these values to the repository, workflow logs, issues, or pull requests.

Do not create GitHub Secrets for:

- VPN user passwords
- The CA private key
- Server private keys
- Generated client credentials
- `GITHUB_TOKEN` (the workflow-provided token is sufficient for GitHub operations)

Those values must remain on the server or be entered during the controlled installation process.

## 7. Add the Deployment Workflow

Create `.github/workflows/deploy-production.yml` with the following content:

```yaml
name: Deploy Production Server Installer

on:
  workflow_dispatch:
  push:
    tags:
      - "v*.*.*"

permissions:
  contents: read

jobs:
  deploy:
    name: Deploy server installer
    runs-on: ubuntu-latest
    environment: production
    timeout-minutes: 10

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Validate installer locally
        shell: bash
        run: |
          set -Eeuo pipefail
          file="ikev2-strongswan-ubuntu.sh"
          bash -n "$file"

      - name: Configure SSH
        shell: bash
        env:
          SSH_PRIVATE_KEY: ${{ secrets.PRODUCTION_SSH_PRIVATE_KEY }}
          KNOWN_HOSTS: ${{ secrets.PRODUCTION_KNOWN_HOSTS }}
        run: |
          set -Eeuo pipefail
          install -d -m 700 "$HOME/.ssh"
          printf '%s\\n' "$SSH_PRIVATE_KEY" > "$HOME/.ssh/production_deploy"
          chmod 600 "$HOME/.ssh/production_deploy"
          printf '%s\\n' "$KNOWN_HOSTS" > "$HOME/.ssh/known_hosts"
          chmod 644 "$HOME/.ssh/known_hosts"

      - name: Upload installer
        shell: bash
        env:
          HOST: ${{ vars.PRODUCTION_HOST }}
          PORT: ${{ vars.PRODUCTION_PORT }}
          USER: ${{ vars.PRODUCTION_USER }}
          INSTALL_DIR: ${{ vars.PRODUCTION_INSTALL_DIR }}
        run: |
          set -Eeuo pipefail
          ssh_args=(-i "$HOME/.ssh/production_deploy" -p "$PORT")
          ssh "${ssh_args[@]}" "$USER@$HOST" \
            "sudo install -d -m 0755 '$INSTALL_DIR'"
          scp "${ssh_args[@]}" \
            ikev2-strongswan-ubuntu.sh \
            "$USER@$HOST:/tmp/ikev2-strongswan-ubuntu.sh"
          ssh "${ssh_args[@]}" "$USER@$HOST" \
            "bash -n /tmp/ikev2-strongswan-ubuntu.sh && \
             sudo install -m 0755 /tmp/ikev2-strongswan-ubuntu.sh \
             '$INSTALL_DIR/ikev2-strongswan-ubuntu.sh' && \
             rm -f /tmp/ikev2-strongswan-ubuntu.sh"

      - name: Verify deployed installer
        shell: bash
        env:
          HOST: ${{ vars.PRODUCTION_HOST }}
          PORT: ${{ vars.PRODUCTION_PORT }}
          USER: ${{ vars.PRODUCTION_USER }}
          INSTALL_DIR: ${{ vars.PRODUCTION_INSTALL_DIR }}
        run: |
          set -Eeuo pipefail
          ssh_args=(-i "$HOME/.ssh/production_deploy" -p "$PORT")
          ssh "${ssh_args[@]}" "$USER@$HOST" \
            "sudo '$INSTALL_DIR/ikev2-strongswan-ubuntu.sh' status"
```

This workflow uploads the installer, applies the DPD-based reconnect policy, and verifies the installation. It does not automatically run `install`, `upgrade`, `uninstall`, or certificate rotation. Those operations should require a deliberate production procedure.

## 8. Recommended Deployment Policy

- Trigger production deployment manually or from signed release tags.
- Require at least one production environment reviewer.
- Use a dedicated non-root SSH account.
- Keep the deployment key limited to this repository and environment.
- Restrict the key in `authorized_keys` if your operational model supports it.
- Keep UDP `500` and `4500` open in the provider firewall.
- Keep SSH `9011` restricted to trusted administration IPs where possible.
- Back up `/var/lib/ikev2-easy-installer/backups/` securely.
- Never commit CA keys, server keys, passwords, `.ikev` profiles, or private SSH keys.
- Run the installer diagnostics after any manual server change:

```bash
sudo /opt/ikev2/ikev2-strongswan-ubuntu.sh diagnostics
```

## 9. Troubleshooting

### Permission denied during deployment

Verify the key, username, and sudo access:

```bash
ssh -i ~/.ssh/ikev2_github_actions_production \\
  -p 9011 admin@155.117.13.45 'sudo -n true'
```

### Host key verification failed

Do not disable verification. Check whether the server was rebuilt, verify its new fingerprint through a trusted channel, and then update `PRODUCTION_KNOWN_HOSTS`.

### Connection timeout

Check all of the following:

- Provider firewall allows TCP `9011` from GitHub-hosted runners or the configured source.
- SSH is listening on `9011`.
- The server has a public route.
- UDP `500` and `4500` are allowed for VPN clients.

### VPN clients cannot connect after deployment

Check the server directly:

```bash
sudo /opt/ikev2/ikev2-strongswan-ubuntu.sh status
sudo /opt/ikev2/ikev2-strongswan-ubuntu.sh diagnostics
sudo ss -lunp | grep -E ':(500|4500)\\b'
```
