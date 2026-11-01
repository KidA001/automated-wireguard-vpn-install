# WireGuard VPN Server

A one-command, interactive deploy for a privacy-focused personal VPN on any VPS:

- **WireGuard** (wg-easy web UI), with optional AmneziaWG obfuscation
- **AdGuard Home** ad/tracker/malware blocking → **Unbound** recursive DNSSEC resolver (no third-party DNS)
- **Authelia** single sign-on with **TOTP 2FA** (plus optional passkeys) protecting every web UI
- **Caddy** for automatic HTTPS
- **No logging** (no DNS query log, no access logs, RAM-only system journal and `/var/log`)
- **Nightly updates** (default 2 AM in your timezone) for the OS and containers, with rollback, auto-reboot when
  required, and an optional email alert if an update fails
- Hardened SSH (key-only recommended), a default-deny nftables firewall, and fail2ban
- Works with any domain and DNS provider, with optional automatic DNS records on Cloudflare
- **Painless rebuilds:** optionally keeps the server's identity in your local encrypted vault, so a rebuilt VPS needs
  no changes on your devices. Full encrypted backups are opt-in

> **Status:** Phase (a) is implemented: core VPN, ad-blocking DNS, hardening, and the no-logs setup. Phases (b) to (d)
> are in progress: the HTTPS login front door, nightly updates, and the extras. Features marked *(coming)* below aren't
> available yet. See [PLAN.md](./PLAN.md) for the full design and roadmap.

---

## Requirements

**VPS**
- Fresh **Debian 13 (trixie)**, x86_64 or arm64
- At least 1 vCPU, 1 GB RAM, 10 GB disk (**2 GB recommended**, especially with the Aggressive blocklist profile)
- Public IPv4 (IPv6 optional but recommended)
- SSH access as root with either a password (only used once) or a key you have already added

**Your machine** (macOS or Linux)
- `ssh`, `python3`, `dig`
- [`uv`](https://docs.astral.sh/uv/) (the deploy script offers to install it)

**Domain**
- Any domain (or delegated subdomain zone) whose DNS you control, at any DNS provider
- **Cloudflare users** can optionally give an API token so the script creates the records for you (see below)

## Quick start

```bash
git clone https://github.com/KidA001/wireguard-vpn-server.git
cd wireguard-vpn-server
./deploy.sh
```

### What it asks

Every prompt shows a recommended default. Answers are saved to the gitignored `config/` folder and pre-filled on
re-runs. Secrets are encrypted with ansible-vault, and the vault password is kept in your OS keychain.

**Always asked**

1. VPS IP address
2. Admin username and sudo password (generated for you by default)
3. Your domain. The DNS provider is detected and you confirm it. Cloudflare only: optional API token *(coming)*.
4. VPN namespace label (default `vpn` → `vpn.<domain>`, `auth.vpn.<domain>`, …)
5. Whether your SSH key is already on the VPS:
   - **Yes:** pick the key and the user it logs in as.
   - **No:** pick an existing key or generate a new one, then enter the root password once.
6. Disable SSH password login? (**strongly recommended**; root SSH login is always disabled)
7. Admin email and web login password
8. Timezone (defaults to your computer's timezone)
9. Ad-blocking profile: Minimal / **Balanced** / Aggressive
10. Email alert if the nightly update fails? (optional Healthchecks.io dead-man's switch)

**Advanced (optional):** after the essentials, the script asks "Use recommended defaults for advanced settings?". If
you answer no, it walks through these:
- SSH and WireGuard ports
- Maintenance time (default 2:00 AM)
- Auto-reboot
- Subdomain labels
- IPv6 mode
- Client isolation
- AmneziaWG obfuscation
- Encrypted DNS for devices off the VPN
- iCloud Private Relay blocking
- Encrypted-DNS bypass blocking
- AdGuard statistics
- "Remember me" duration
- Passkeys
- Keep server identity in the vault (default on)
- Full encrypted backups (default off)
- fail2ban

### What it does

1. Shows the server's host-key fingerprint for you to confirm and pins it.
2. Creates your admin user and verifies key login works. Then it disables root SSH login, and password login if you
   chose that.
3. Sets up DNS records:
   - **Cloudflare with a token:** shows exactly which records it will create or change and only writes them after you
     confirm.
   - **Anything else:** prints the records for you to add.

   Then it waits until they resolve.
4. Installs and configures everything, then runs verification checks.

Re-run `./deploy.sh` at any time. It is idempotent.

**Useful flags:**
- `--staging`: use Let's Encrypt test certificates. Use this while experimenting, to avoid rate limits.
- `--non-interactive`: reuse the saved answers with no prompts.
- `--reconfigure`: ask every question again.
- `--tags <role>`: re-apply only some components.
- `--check`: dry run.

## DNS records

Shown with the default `vpn` namespace. Replace the IPs with your VPS's. If your provider can proxy traffic (e.g.
Cloudflare's orange cloud), set every record to **DNS only**.

| Type | Name | Value |
|------|------|-------|
| A | `vpn` | VPS IPv4 |
| AAAA | `vpn` | VPS IPv6 (if available) |
| CNAME | `auth.vpn` | `vpn.<your-domain>` |
| CNAME | `wg.vpn` | `vpn.<your-domain>` |
| CNAME | `adguard.vpn` | `vpn.<your-domain>` |
| CNAME | `dns.vpn` | `vpn.<your-domain>` |

Recommended manual steps (never automated):
- Turn on **DNSSEC** for the zone. If your registrar is not your DNS provider, also add the DS record at the registrar.
- Add **CAA** records allowing only `letsencrypt.org` and `sectigo.com` to issue certificates.

### Optional: Cloudflare API token

Create a token so `deploy.sh` can manage the records above:

1. Go to Cloudflare dashboard → **My Profile → API Tokens → Create Token → Create Custom Token**.
2. Set permissions to **Zone → DNS → Edit**.
3. Set zone resources to **Include → Specific zone → your domain**.
4. Create the token and paste it when `deploy.sh` asks.

The token is stored only in the encrypted vault on your machine and is never copied to the VPS. The script never
deletes records and always asks before it changes anything.

## After deploying

**Right now (Phase a):** the web UIs are reachable only through an SSH tunnel:

```bash
ssh -L 51821:127.0.0.1:51821 vpn   # then open http://localhost:51821 (WireGuard UI)
ssh -L 3000:127.0.0.1:3000 vpn     # then open http://localhost:3000  (AdGuard UI)
./deploy.sh --show-secret wg_easy_password   # login: admin
./deploy.sh --show-secret adguard_password   # login: admin
```

In the WireGuard UI, add a client and scan its QR code with the WireGuard app. Then check
[dnsleaktest.com](https://dnsleaktest.com) (extended test) and [ipleak.net](https://ipleak.net).

AdGuard settings changed in its web UI are overwritten on the next deploy. Put allowlist or custom rules in
`config/settings.yml` instead, for example:

```yaml
adguard_custom_rules:
  - "@@||example.com^"   # always allow example.com
```

**After Phase (b)** *(coming)*:

| URL | Purpose |
|-----|---------|
| `https://auth.vpn.<domain>` | Sign in (password + TOTP) |
| `https://wg.vpn.<domain>` | Manage WireGuard clients |
| `https://adguard.vpn.<domain>` | Manage ad blocking |
| `dns.vpn.<domain>` | Encrypted DNS (DoH/DoT/DoQ) for devices off the VPN. The ClientID is printed by the deploy |

**First login** *(coming in Phase b)*:
1. At the end of the first deploy, `deploy.sh` shows a **TOTP QR code** in your terminal. Scan it into your
   authenticator app.
2. Sign in at `auth.vpn.<domain>` with your password and a TOTP code.
3. Optionally, register a passkey in the Authelia settings. The confirmation code for that is shown by
   `ssh vpn sudo vpn-status --auth-code`.

**Break-glass:**
- If single sign-on to wg-easy breaks, `ssh vpn sudo vpn-breakglass wg-easy` temporarily turns its password login
  back on. The next deploy reverts it.
- If SSH breaks, use your VPS provider's web console with the root password stored in your vault:
  `./deploy.sh --show-secret root_password`.

## Operations

The examples below assume the default `vpn` SSH host alias that `deploy.sh` adds to `~/.ssh/config`.

| Command | What it does |
|---------|--------------|
| `./status.sh` | Health of services, timers, firewall, and last update |
| `ssh vpn sudo vpn-debug on` | Temporarily enable logs for debugging (reverts on `off` or on reboot) |
| `ssh vpn sudo vpn-maintenance` | *(coming)* Run the nightly update now (OS, then containers, then reboot if needed) |
| `./backup.sh` | *(coming)* Pull an encrypted full-state backup (only if full backups are enabled) |
| `./deploy.sh --tags <role>` | Re-apply a single component |

## Rebuilding from scratch

1. Rebuild the VPS with Debian 13.
2. Remove the old host key: `ssh-keygen -R <vps-ip>`.
3. Update the `A`/`AAAA` records if the IP changed. With a saved Cloudflare token, `deploy.sh` offers to do this
   for you.
4. Run `./deploy.sh`.

What survives a rebuild depends on your backup settings:

| Setting | After a rebuild |
|---------|-----------------|
| **Identity in vault** (default) | Devices reconnect unchanged and your TOTP entry still works. Peers added in the UI since the last deploy are lost, so run `./deploy.sh --tags identity` after adding devices. Passkeys must be registered again |
| **Full backups** (opt-in) | Everything above, plus AdGuard customisations and Authelia state. The deploy offers to restore the newest backup |
| Neither | New server key: re-add every device and re-enroll TOTP |

Keep your vault password (stored in your keychain; the wizard offers to show it once) in a password manager.
Without it, the vault can't be decrypted.

## Security & privacy summary

See [PLAN.md](./PLAN.md) for details:
- Threat model: §1.1
- Firewall, logging policy, SSH hardening, DNS hardening, auth, and the update strategy: §6–§11
- Backups: §13

This is a **privacy** VPN, not an anonymity tool. Your VPS provider can still see your traffic leave the server.

## Development & testing

You need [Lima](https://lima-vm.io) (`brew install lima`). Everything runs against throwaway local VMs and never
touches your real `config/` or keychain.

```bash
tests/vm/deploy-vm.sh --fresh   # reimage a local Debian 13 "VPS" VM and run the real deploy
tests/vm/deploy-vm.sh           # re-run (should report changed=0)
uv run pytest -q                # wizard unit tests
pre-commit run --all-files      # ruff, yamllint, ansible-lint, shellcheck, pytest
```

`tests/vm/client.yaml` and `tests/vm/client-checks.sh` add a second VM that connects as a WireGuard client and checks
the following end to end:
- the tunnel works
- DNS goes through AdGuard and blocking works
- forced DNS and the DoT block work
- the admin UIs aren't exposed on the tunnel
- IPv6 doesn't leak

See PLAN.md §19.

