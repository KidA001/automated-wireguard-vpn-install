# WireGuard VPN Server — Implementation Plan

> Status: **v3, Phase (a) implemented** and validated in a local VM (see §19) · Last updated: 2026-11-01
>
> This plan comes from the decision interview and a third-party review (see [Decision log](#16-decision-log)). Once
> it is approved, the implementation follows this document, using the phased roadmap in §15. If the implementation
> changes, update this file in the same change.

---

## 1. Goals (requirements → solution)

| # | Requirement | Solution |
|---|-------------|----------|
| 1 | WireGuard | **wg-easy v15** (`ghcr.io/wg-easy/wg-easy:15`) — WireGuard server + web UI. Optional AmneziaWG obfuscation can be turned on with a flag (off by default) |
| 2 | Ad-blocking DNS | **AdGuard Home** (self-hosted, not NextDNS), fully pre-configured with a blocklist profile you choose at deploy: Minimal, **Balanced** (default), or Aggressive |
| 3 | Recursive DNS | **Unbound**, pure recursion to the root servers. No forwarders and no third-party fallback. DNSSEC validation on |
| 4 | No logging | DNS query log off; Caddy access logs off; container logs off; journald is RAM-only (1h retention, ≤16 MB); **`/var/log` is a RAM disk** (so login records and apt logs never reach disk); Authelia's auth records are purged; a `vpn-debug` toggle reverts automatically on reboot |
| 5 | Secure auth + 2FA | **Authelia v4.39** (password + TOTP, with optional passkeys/WebAuthn). Works as a forward-auth gate in front of every UI and as an OIDC provider for wg-easy, so you sign in once for everything |
| 6 | Nightly updates at 2 AM | A single sequential `vpn-maintenance` run at the maintenance time T (default 02:00 in the timezone you choose): OS updates → container updates with digest pinning, rollback and quarantine of bad images → reboot only if needed → optional Healthchecks.io ping that emails you if the run fails or doesn't happen |
| 7 | Always running | systemd-managed compose stack, `restart: unless-stopped`, healthchecks, a host-side timer that restarts unhealthy containers, Docker `live-restore` |
| 8 | Automated, interactive deploy | `./deploy.sh` on your machine. It sets up Ansible in a local venv, asks the essential questions plus optional advanced ones (§4), and runs an idempotent playbook against any VPS. It does not depend on any particular domain or DNS provider |
| 9 | Secure SSH | Uses an existing or newly generated key, pins the host key, creates a non-root sudo user, disables root login, makes key-only login the recommended default, hardens sshd, and adds nftables rate-limiting and fail2ban |
| 10 | Network/security best practice | Default-deny nftables, sysctl hardening, AppArmor, least-privilege containers, no Docker socket in any container, HSTS/TLS 1.3, CAA + DNSSEC on the zone |
| 11 | Linux user/app best practice | Non-root admin with password-protected sudo, packages from signed official repos only, everything under `/opt/vpn` owned by root, secrets mode 0600 |
| 12 | Subdomains | You give your own zone (e.g. `example.com`). The defaults are `vpn.<zone>` (WireGuard endpoint) plus `auth.vpn.<zone>`, `wg.vpn.<zone>`, `adguard.vpn.<zone>`, and `dns.vpn.<zone>`. The labels can be changed. The DNS provider is detected and you confirm it. On Cloudflare, records can optionally be created through the API after you confirm the changes |
| 13 | Root password → key-only | If your key is already on the VPS, it's used directly. Otherwise the root password is used once to install a key. Key login is verified first, then root login is always disabled and password login is disabled (strongly recommended, and you're asked) |
| 14 | Extra DNS privacy | Forced DNS, encrypted-DNS bypass blocking, Private Relay blocking, public DoH/DoT/DoQ limited to an allowlist of secret ClientIDs, local name rewrites, no ECS, QNAME minimisation |
| — | Painless rebuilds | Optional: the server's identity (WireGuard key, peers, TOTP secret) is kept in the local vault, so a rebuilt VPS needs no client changes. A full encrypted state backup/restore is opt-in (§13) |

### 1.1 Threat model

**What this protects against:**
- Your ISP, hostile or public Wi-Fi, and on-path observers seeing or changing your traffic and DNS.
- Ad, tracker and malware domains.
- Third-party DNS resolvers building a profile of your browsing.
- Opportunistic internet scanning and brute-forcing of the server and its admin UIs.
- Forensic recovery of activity logs from the VPS disk.

**What it does not protect against:**
- **The VPS provider.** It controls the hypervisor and can see traffic leaving the server.
- **Linkability.** The VPS IP is a stable identifier tied to your account and payment. This is a privacy VPN for you,
  not an anonymity network. Use Tor for anonymity.
- **Compromised client devices.**
- **Legal compulsion** of the VPS provider.
- **Browser fingerprinting and logged-in tracking**, such as cookies or Google accounts.

Trade-off decisions in this plan favour (1) not storing data over convenience, (2) small attack surface over features,
and (3) automated recovery over manual tuning.

---

## 2. Architecture

```
                         Internet
                            │
     ┌──────────────────────┼──────────────────────────────────────────┐
     │ VPS (Debian 13)      │  nftables default-deny (table inet vpn)   │
     │                      │                                           │
     │  :22/tcp  sshd (key-only, rate-limited, fail2ban)                │
     │  :80,:443/tcp,:443/udp ─► Caddy ──forward_auth──► Authelia :9091 │
     │                            │  auth.vpn.*  ─► Authelia            │
     │                            │  wg.vpn.*    ─► wg-easy UI  :51821  │
     │                            │  adguard.vpn.* ► AdGuard UI :3000   │
     │                            │  dns.vpn.*/dns-query ► AdGuard DoH  │
     │  :853/tcp+udp ─► AdGuard DoT/DoQ (ClientID allowlist)            │
     │  :51820/udp ─► wg0 (wg-easy, kernel WireGuard)                   │
     │                      │                                           │
     │  wg0 clients ──:53 (DNAT-forced)──► AdGuard :53 ──► Unbound      │
     │                                            127.0.0.1:53 ──► root │
     │  wg0 clients ──masquerade (v4) / NAT66 (v6 ULA)──► Internet      │
     └──────────────────────────────────────────────────────────────────┘
```

### Key design choices

- **Every container runs with `network_mode: host`, and Docker's firewall management is turned off**
  (`"iptables": false, "ip6tables": false`, no default bridge).
  - *Why:* a single nftables ruleset that we own controls all traffic. That covers default-deny, forced DNS DNAT, client
    isolation, NAT44/NAT66, and rate limits. It avoids the well-known "Docker bypasses the firewall" problem, and it avoids
    mixing iptables-legacy and nft between wg-easy's hooks and the host.
  - Internal services bind to `127.0.0.1` only: Authelia `:9091`, AdGuard UI `:3000`, wg-easy UI `:51821`, Unbound
    `:53`. AdGuard's DNS binds only to the tunnel addresses (§9).
  - **wg-easy firewall hooks:** wg-easy normally adds its own iptables rules. The **primary approach** is to replace
    its PostUp/PostDown hooks with no-ops so that nftables is the only firewall. They are written straight into
    wg-easy's SQLite database (or set through its API) **before** password login is disabled for OIDC, because the
    automation needs that login (§10).
    - **Fallback** if this proves brittle: leave wg-easy's default rules in place. With the shared host network they
      sit alongside our nftables table, and since a drop in any table is final, our default-deny, client-isolation
      and forced-DNS rules still apply. Spike this in Phase (a).
  - **Network settings for wg-easy:** with the shared host network, Docker doesn't allow per-container network
    settings (sysctls). IP forwarding and `src_valid_mark` are set **on the host** by the `firewall` role, and wg-easy's
    compose service has no `sysctls:` block.
  - **Port 53:** some cloud images run `systemd-resolved`, which already holds port 53. The `base` role disables it
    (and masks it) before AdGuard starts.
  - *Trade-off:* containers share the host network namespace, so isolation comes from loopback binding plus
    least-privilege settings rather than from separate namespaces. Because of this, AdGuard keeps an admin password
    (§9, §10).
- **Unbound runs from Debian's apt package, not a container.** There is no official Unbound image, the Debian package
  gets security updates from the Debian security team through unattended-upgrades, and the VPS uses it as its own
  resolver too. *(This is a deliberate exception to "Docker for everything". Flag it if you disagree.)*
- **Docker images and version tracking:**

  | Image | Tracks | Why |
  |-------|--------|-----|
  | `caddy:2` | major | Official image publishes major tags |
  | `ghcr.io/wg-easy/wg-easy:15` | major | Major tag published |
  | `authelia/authelia:4` | major | Authelia publishes `4` as well as `4.39`. Tracking `4` gets new minors (e.g. 4.40) instead of going stale on 4.39 |
  | `adguard/adguardhome` | **v0.107.x, resolved to a digest** | AdGuard publishes only `vX.Y.Z` and `latest`. `latest` will jump to v1.0 when it goes stable, which would be an unattended major upgrade. `vpn-maintenance` picks the newest `v0.107.*` tag from the registry instead |

  - Every update records the exact image digests in `/opt/vpn/images.lock` (§11).
  - A weekly check reports any *new major* version available in `vpn-status` and the Healthchecks message. Majors are
    adopted deliberately by updating `group_vars` and re-deploying.
- **Memory budget (1 GB VPS):**
  - **zram** compressed swap (50% of RAM) plus `vm.swappiness=100`, with no swapfile on disk (privacy).
  - Container `mem_limit`s: AdGuard 256 MB, wg-easy 192 MB, Authelia 128 MB, Caddy 128 MB.
  - Unbound cache: `msg-cache-size 16m`, `rrset-cache-size 32m`.
  - With the Aggressive blocklist profile, the README recommends 2 GB.

---

## 3. Domain, DNS provider & records

Nothing is tied to one domain or provider. In the examples, `<zone>` is the domain whose DNS you manage. It can be an
apex domain such as `example.com` or a delegated subdomain zone such as `home.example.net`. `<ns>` is the VPN
namespace label (default `vpn`). If you leave it blank, the hosts go directly under the zone.

### Hostnames

| Host | Default | Purpose |
|------|---------|---------|
| Endpoint | `vpn.<zone>` | WireGuard endpoint; A/AAAA → VPS |
| Auth | `auth.vpn.<zone>` | Authelia portal |
| WireGuard UI | `wg.vpn.<zone>` | wg-easy |
| AdGuard UI | `adguard.vpn.<zone>` | AdGuard Home |
| Encrypted DNS | `dns.vpn.<zone>` | DoH/DoT/DoQ |

The `auth`/`wg`/`adguard`/`dns` labels can be renamed in advanced mode.

### Records

| Type | Name | Value | Notes |
|------|------|-------|-------|
| A | `<ns>` | `<VPS IPv4>` | If the provider proxies traffic (e.g. Cloudflare orange cloud), it must be **DNS only** |
| AAAA | `<ns>` | `<VPS IPv6>` | Only if the VPS has public IPv6 |
| CNAME | `auth.<ns>` | `<ns>.<zone>` | DNS only |
| CNAME | `wg.<ns>` | `<ns>.<zone>` | DNS only |
| CNAME | `adguard.<ns>` | `<ns>.<zone>` | DNS only |
| CNAME | `dns.<ns>` | `<ns>.<zone>` | DNS only |

Recommended manual extras (shown as instructions only, never automated):
- **DNSSEC** on the zone, plus the DS record at the registrar if the registrar isn't the DNS provider.
- **CAA** records: `0 issue "letsencrypt.org"` and `0 issue "sectigo.com"` (ZeroSSL).

### Provider detection & automation

1. After you enter the zone, `deploy.sh` looks up its **NS records** and maps them to a provider:
   - `*.ns.cloudflare.com` → Cloudflare
   - `awsdns` → Route 53
   - `porkbun` → Porkbun
   - `registrar-servers.com` → Namecheap
   - `domaincontrol.com` → GoDaddy
   - `googledomains`/`squarespace` → Squarespace
   - `digitalocean`, `linode`, `hetzner`, `vultr`, and others
   - anything else → "Unknown"
2. It shows "Detected DNS provider: X — correct? [Y/n]". If you answer no, you choose from the list or type a name.
3. **Cloudflare (API supported):** it asks whether you want to give an API token for automatic records. This is
   optional, and if you skip it you add the records by hand.
   - **Token scope:** *Zone → DNS → Edit* on that one zone only. The README links to step-by-step token creation.
     `deploy.sh` checks the token with `GET /user/tokens/verify` and confirms it can see the zone. If either check
     fails, it stops with instructions.
   - **Storage:** the token is kept in the encrypted vault on **your machine only**, so later deploys (e.g. a new VPS
     IP) can update the records. It is **never copied to the VPS**.
   - **Confirmation first:** it shows a change table for the records above (CREATE, UPDATE old → new, or unchanged)
     and asks "Apply these DNS changes? [y/N]". Nothing is written without a yes.
   - **Safety:** records are always `proxied: false` and tagged with the comment `managed-by: wireguard-vpn-server`.
     It never deletes records. If an existing record has a different type at the same name (e.g. an A record where a
     CNAME is needed), it asks again explicitly before replacing it.
4. **All other providers (or no token):** it prints the exact record table with the IPs filled in and waits.
5. **DNS gate (all paths):** it polls public resolvers (1.1.1.1, 8.8.8.8, 9.9.9.9) until every hostname resolves to
   the VPS. You can skip this, but certificates won't be issued until the records resolve.

Certificates always use the **HTTP-01/TLS-ALPN** challenge for each hostname, handled automatically by Caddy. No DNS
API token is ever stored on the VPS.

---

## 4. Deploy workflow (`./deploy.sh`)

Run from the repo root on macOS or Linux. It is interactive and safe to re-run. Every answer is saved to the
gitignored `config/settings.yml` and offered as the default on the next run. Every prompt shows its recommended
default.

**Implementation:** `deploy.sh` is a thin bash launcher. It makes sure `uv` is available and then runs the wizard,
a small **Python CLI** (`wizard/`, run with `uv run`, dependencies pinned in `pyproject.toml`/`uv.lock`).
- *Why Python:* the wizard handles NS detection, Cloudflare API calls, keychain access, input validation, saved
  defaults and change tables. That logic would be fragile in bash, and Python can be unit-tested.
- *Libraries:* `questionary`/`rich` for prompts, `httpx` for the API, `dnspython` for lookups.
- The wizard writes the settings and vault files and then runs `ansible-playbook`.

**Flags:**

| Flag | Effect |
|------|--------|
| `--tags <role,…>` | Run only the named roles |
| `--check` | Dry run |
| `--non-interactive` | Use the saved settings with no prompts; fails if anything required is missing |
| `--staging` | Use Let's Encrypt **staging** certificates, which avoid the 5-per-week duplicate-certificate rate limit during wipe-and-redeploy testing. Running without it later switches back to real certificates |
| `--reconfigure` | Ask every question again, ignoring saved defaults |
| `--show-secret <name>` | Decrypt and print one vault entry (e.g. `root_password` for provider-console break-glass) |

### 4.1 Preflight (local)

- Check for `ssh`, `python3`, `dig`/`nslookup`, and `uv`. If uv is missing, offer to install it (Homebrew or the
  official installer).
- Create `.venv/` with a pinned `ansible-core` and install the collections from `requirements.yml`
  (`community.general`, `ansible.posix`).
- Ansible connection settings (`ansible.cfg`): `pipelining = True` and SSH `ControlMaster=auto` /
  `ControlPersist=60s`. Together these make an entire run use only a few SSH connections, so it stays well under the
  server's SSH rate limit (§6).

### 4.2 Essential prompts (always asked)

| # | Prompt | Default / behaviour |
|---|--------|---------------------|
| 1 | VPS public IPv4 | — (IPv6 is detected on the server later) |
| 2 | SSH access — see §4.4 | — |
| 3 | Admin username + sudo password | Your local username; the sudo password is generated, or you can enter your own |
| 4 | Disable SSH password login? | **Yes (strongly recommended)**. If you answer no, you get a warning and have to confirm a second time |
| 5 | Domain (zone) → detected DNS provider (confirm or override) → Cloudflare only: API token for automatic records? | See §3 |
| 6 | VPN namespace label | `vpn` (blank = directly under the zone) |
| 7 | Admin email + Authelia password | The email is your Authelia identity and the wg-easy SSO link. The password is entered twice and must be at least 12 characters |
| 8 | Timezone | **Detected from this computer** (macOS/Linux `/etc/localtime` → IANA name, e.g. "America/Chicago (detected)"). Any IANA name is accepted and checked against `zoneinfo`; falls back to `UTC` if detection fails |
| 9 | AdGuard blocklist profile | Minimal / **Balanced** / Aggressive (§9) |
| 10 | Nightly-update dead-man's switch (Healthchecks.io)? | **No**. If yes, it asks for the ping URL and shows short signup steps |

### 4.3 Advanced prompts

The script asks "Use recommended defaults for advanced settings? [Y/n]". If you answer no, it walks through each item
with its default and a one-line explanation.

| Setting | Default |
|---------|---------|
| SSH port | 22 |
| WireGuard UDP port | 51820 |
| Maintenance time (OS updates; containers +15 min, reboot +45 min) | 02:00 |
| Auto-reboot when an update requires it | Yes |
| Subdomain labels (auth / wg / adguard / dns) | as shown |
| IPv6 mode | auto (NAT66 if the VPS has IPv6, otherwise blackhole) |
| Client isolation | Yes |
| AmneziaWG obfuscation | No |
| Public DoH/DoT/DoQ with ClientID allowlist | Yes |
| Block iCloud Private Relay on the VPN | Yes |
| Block encrypted-DNS bypass (DoT/DoH/VPN lists) | Yes |
| AdGuard aggregate statistics (24h) | Yes |
| Authelia remember-me | 30d (`0` = off) |
| Passkeys / WebAuthn as an extra 2FA method (phishing-resistant) | Yes (TOTP stays the default method) |
| Keep server identity in the vault (WireGuard key + peers + TOTP secret, so rebuilds need no client changes) | Yes |
| Full encrypted state backups (`./backup.sh` + restore offer on deploy) | No |
| fail2ban | Yes |

### 4.4 SSH bootstrap flow

1. "Is your SSH public key already authorized on the VPS? [y/N]"
   - **Yes:**
     - Pick a key from the `~/.ssh/*.pub` list, or give another path.
     - "Which user does it log in as? [root]".
     - Test key login. If it fails, offer to retry or to switch to the "No" path.
   - **No:**
     - Choose between an existing local key (picked from the list) and a newly generated dedicated key
       `~/.ssh/id_ed25519_<ns>_<zone>`. A new key can have an optional passphrase; on macOS it is added to the
       Keychain with `ssh-add --apple-use-keychain`.
     - Ask for the root password once. It is kept in memory only and never written to disk. Install the public key
       for root and test key login.
2. **Host-key pinning:**
   - Fetch the host key with `ssh-keyscan` and show its SHA256 fingerprint.
   - You confirm it, or compare it with the provider console first.
   - Pin it in `~/.ssh/known_hosts`. All later connections use `StrictHostKeyChecking=yes`.
3. **`bootstrap.yml`** runs with the working login:
   - Create the admin user with the same public key, sudo with a password, and no `docker` group.
4. **Verify:**
   - A fresh connection as the admin user succeeds with the key, and `sudo -v` succeeds with the vault password.
5. **Lock down** (only after step 4 passes):
   - **Always** set `PermitRootLogin no`.
   - If you answered yes to prompt 4, also set `PasswordAuthentication no` and `KbdInteractiveAuthentication no`.
   - **Rotate the root password** to a random 32-character value stored in the vault. The provider's original root
     password may have been emailed or shown in a panel. The new one is only for the provider's web console as a
     break-glass login.
   - Reload sshd, then verify again over a new connection while the old session stays open as a fallback.
   - If verification fails, the change is rolled back automatically.
6. Write a `Host <ns>` block to `~/.ssh/config` (after you confirm): HostName, User, Port, IdentityFile,
   `IdentitiesOnly yes`.

On later runs, the script sees that the admin user's key login already works and skips the bootstrap entirely.

### 4.5 Secrets

- Generated or prompted, and stored in `config/vault.yml`, encrypted with **ansible-vault**.
- The vault password is stored in the macOS Keychain (`security add-generic-password`) or, on Linux,
  `secret-tool`/libsecret. If neither is available, it is prompted for.
- Contents:
  - sudo password and rotated root password
  - Authelia admin password (argon2id-hashed on the server)
  - Authelia session/storage/JWT/OIDC HMAC secrets
  - OIDC RSA key
  - wg-easy OIDC client secret
  - AdGuard admin password (injected by Caddy, §10)
  - DoH/DoT ClientIDs
  - **Server identity** (if enabled, §13): WireGuard server private key, ULA prefix, peer list (names, keys, PSKs,
    addresses), Authelia TOTP secret
  - Backup encryption key (if full backups are enabled, §13)
  - Cloudflare API token (optional, local use only)
  - Healthchecks ping URL (optional)
- The wizard reminds you that the vault plus its keychain password are what make rebuilds painless. It offers to
  print the vault password once so you can store it in your password manager.

### 4.6 Run

1. **Restore offer:**
   - If full backups are enabled and a backup exists in `backups/`, the wizard asks "Restore state from
     `<latest>`? [Y/n]" (§13).
   - Otherwise, if server identity is stored in the vault, it is reapplied automatically.
2. **DNS:**
   - Records are created through Cloudflare (after you confirm), or printed for you to add manually.
   - Then the DNS gate runs (§3).
3. **Main playbook** as the admin user (`become` with the vault sudo password). Roles are listed in §5.
4. **Verify** (§12).
5. **Summary:**
   - URLs and first-login steps.
   - **On first deploy only:** a terminal QR code for your TOTP secret, so you can scan it into your authenticator app.
     It is pre-generated and kept in the vault when identity is enabled, so it survives rebuilds.
   - AdGuard DoH/DoT/DoQ client strings.
   - Recommended manual DNSSEC/CAA steps.
   - The `Host` alias to use.
   - Any warnings, such as staging certificates or password SSH left on.

`./status.sh` runs `vpn-status` over SSH.

---

## 5. Ansible roles (run in this order)

| Role | Responsibility |
|------|----------------|
| `bootstrap` | (first run) Admin user, sudo, authorized_keys, verified switch: root login always off, password login off if chosen |
| `base` | Hostname, timezone, locale, chrony (accurate time matters for TOTP), apt sources, minimal packages, remove unneeded services, **disable and mask `systemd-resolved`**, **zram swap**, sysctl hardening, AppArmor enforced, core dumps off, `/dev/shm` with `nodev,nosuid,noexec`, `/tmp` with `nodev,nosuid` (executable, because DKMS/apt scripts need it) |
| `ssh` | Hardened `sshd_config.d/` (see §8) |
| `logging` | journald volatile/1h/16 MB, no rsyslog, **`/var/log` on tmpfs**, `wtmp`/`btmp`/`lastlog` disabled, Authelia DB purge timer, `vpn-debug` tool (§7) |
| `firewall` | nftables ruleset (§6), forwarding sysctls on the host (`ip_forward`, `src_valid_mark`, v6 forwarding), IPv6 auto-detection |
| `fail2ban` | `backend=systemd`, `banaction=nftables-multiport`, jails for `sshd` + `authelia` |
| `docker` | Docker CE from download.docker.com (signed repo), `daemon.json` (iptables off, live-restore, log-driver none, no-new-privileges default) |
| `unbound` | apt package, config in §9 |
| `adguard` | Container + a fully templated `AdGuardHome.yaml` (§9). Random admin password from the vault, which Caddy injects (§10), so you still sign in only once |
| `authelia` | Container, file user backend (argon2id), SQLite storage, filesystem notifier, TOTP (+ optional WebAuthn), TOTP secret pre-seeded from the vault, OIDC client for wg-easy, access-control rules, IP-mode brute-force regulation |
| `wg_easy` | Container with unattended `INIT_*` first-run variables (+ server key/peers restored from the vault if identity is enabled). The order matters: start → set hooks (§2) → configure OIDC → verify OIDC login → set `OAUTH_DISABLE_PASSWORD=true`. `TRUSTED_PROXIES=127.0.0.1`, optional AmneziaWG (DKMS module + `EXPERIMENTAL_AWG`) |
| `caddy` | Container, Caddyfile with forward_auth, AdGuard auth-header injection, ClientID-required DoH route, security headers, HTTP/3, access logs off, ACME staging switch, certificate sync to AdGuard for DoT/DoQ |
| `stack` | `/opt/vpn/compose.yml` + the `vpn-stack.service` systemd unit (After=docker, network-online, wg0) |
| `maintenance` | unattended-upgrades configured to install only when called, the sequential `vpn-maintenance` service + timer (§11), digest lockfile + quarantine, `vpn-heal` timer, optional Healthchecks ping |
| `identity` | (if enabled) Exports the current server identity (WireGuard key, peers, TOTP secret) back to the vault on your machine at the end of every deploy, so peers added in the UI are kept (§13) |
| `backup` | (if full backups are enabled) Installs `vpn-backup-export`, which `./backup.sh` calls over SSH (§13) |
| `tools` | `vpn-status`, `vpn-debug`, `vpn-maintenance` (manual run), and MOTD |
| `verify` | Post-deploy assertions (§12) |

### Filesystem layout on the VPS

```
/opt/vpn/                 root:root 0750
  compose.yml
  caddy/{Caddyfile,data/,config/}
  authelia/{configuration.yml,users.yml (0600),db.sqlite3,notification.txt}
  adguard/{conf/AdGuardHome.yaml,work/,certs/}
  wg-easy/                (wg-easy DB/keys, 0700)
  .env                    (0600, secrets for compose)
  images.lock             (pinned digests + quarantine list)
/usr/local/sbin/vpn-{status,debug,maintenance,heal,backup-export}
/etc/nftables.conf, /etc/unbound/unbound.conf.d/vpn.conf
```

---

## 6. Firewall (nftables, `table inet vpn`)

- **input** (policy drop): established/related; loopback; ICMP/ICMPv6 with rate limits (ND and PMTU are required);
  `tcp/<ssh_port>` with `ct state new` limited per source IP to **20/minute, burst 10** (a meter); `80,443/tcp`; `443/udp`;
  `853/tcp,udp`; `<wg_port>/udp`; from `wg0` only: `53/tcp,udp` (AdGuard). Everything else is dropped silently, with no
  log rule.
- **forward** (policy drop): `wg0 → public iface` allowed; established/related back; **`wg0 → wg0` dropped** (client
  isolation); wg0 → VPS LAN/metadata ranges (169.254.169.254, RFC1918 on the public side) dropped; **wg0 →
  `tcp/udp 853` dropped, except to the VPS's own public IPs** (blocks DoT/DoQ bypass while still allowing our own
  `dns.<ns>` endpoint).
- **Deployer allowance:**
  - During a deploy, the wizard adds your current public IP to a timed nftables set (`deployers`, 1h timeout). That
    IP is exempt from the SSH rate limit, and the wizard sets fail2ban `ignoreip` for it for that run.
  - The entry expires on its own and nothing is stored.
  - Combined with Ansible pipelining and ControlPersist (§4.1), this keeps deploys from locking you out.
- **nat prerouting:** `iifname wg0 udp/tcp dport 53 → dnat to the wg0 address :53` (forced DNS, v4 + v6).
- **nat postrouting:** masquerade `wg0 v4 subnet → public iface`. For IPv6: if the VPS has global IPv6, use
  **NAT66** masquerade of the ULA `fdxx::/64`. Otherwise **blackhole** (clients still route `::/0` into the tunnel,
  which is rejected with icmpv6 admin-prohibited so apps fall back to v4 quickly).
- fail2ban adds its own `table inet f2b-table` (separate, no conflicts).
- Because Docker's iptables management is off, no `DOCKER-*` chains exist.

---

## 7. Logging & privacy policy

| Component | Setting |
|-----------|---------|
| journald | `Storage=volatile`, `RuntimeMaxUse=16M`, `MaxRetentionSec=1h`, `ForwardToSyslog=no` |
| rsyslog / logrotate files | rsyslog not installed, so there is no `/var/log/auth.log` or `syslog` on disk |
| `/var/log` | Mounted as **tmpfs** (64 MB, `nodev,nosuid,noexec`), so anything that still writes there (apt, dpkg, unattended-upgrades, Docker) stays in RAM and is lost on reboot |
| Login records | `wtmp`, `btmp` and `lastlog` (which record every SSH login and source IP) are linked to `/dev/null`, and `pam_lastlog` is disabled. `last`/`lastb` show nothing |
| sshd | `LogLevel INFO` → volatile journal (fail2ban reads it) |
| Docker | Default `log-driver: none`. **Authelia only** uses `log-driver: journald` (warn level) so fail2ban can see failed logins |
| AdGuard | `querylog.enabled: false`, `statistics.enabled: true` + `interval: 24h` (aggregate counts only, no per-client log), `anonymize_client_ip: true` |
| Unbound | `verbosity: 0`, `log-queries: no`, `log-replies: no`, `log-servfail: no`, `use-syslog: no` |
| Caddy | No `log` directive (access logs off); global log level ERROR |
| wg-easy | Stores only peer config plus last-handshake/transfer counters (needed for the UI) |
| Authelia | Its SQLite DB keeps an authentication log (username + IP) and ban records for regulation. A `vpn-authelia-purge` timer (every 15 min) deletes auth-log rows older than `find_time` and **expired** bans. Active bans are kept until they expire. No audit log export |
| Shell history | `HISTFILE=/dev/null` for root; not changed for the admin user |

**`vpn-debug on [--hours N]`** puts drop-ins in **`/run`** (tmpfs): `/run/systemd/journald.conf.d/` (raises
retention to 24h and 256 MB) and a compose override in `/run/vpn/` that turns on journald logging for all containers,
sets Authelia/AdGuard/Caddy to debug, and sets Unbound verbosity 2. `vpn-debug off` removes them. **A reboot always
reverts them**, because `/run` is wiped. An optional `--hours` sets a systemd-run timer that reverts automatically.

---

## 8. SSH & user hardening

- Admin user (prompted; defaults to your local username). Groups: `sudo` only (**not** `docker`, because docker group
  membership is equivalent to root without a password). `sudo` requires the password, with a 5-minute timestamp
  timeout and `use_pty`.
- `sshd_config.d/10-hardening.conf` (sorts before cloud-init's `50-*` drop-ins, so our settings take precedence):
  `PermitRootLogin no` (**always**), `PasswordAuthentication no` + `KbdInteractiveAuthentication no` +
  `AuthenticationMethods publickey` (**if you chose to disable password login, which is the recommended default**),
  `PubkeyAuthentication yes`,
  `AllowUsers <admin>`, `MaxAuthTries 3`, `LoginGraceTime 20`, `MaxStartups 10:30:60`, `ClientAliveInterval 300`,
  `X11Forwarding no`, `AllowAgentForwarding no`, `AllowTcpForwarding local` (lets you tunnel to internal UIs for
  break-glass access), `PermitTunnel no`.
- If password login stays on, fail2ban's sshd jail is made stricter (3 failures, 24h ban), and `vpn-status` shows a
  warning.
  - Modern crypto only: `KexAlgorithms mlkem768x25519-sha256,sntrup761x25519-sha512,curve25519-sha256`,
    `HostKeyAlgorithms ssh-ed25519`, AEAD ciphers, ETM MACs.
  - The RSA/ECDSA host keys are removed; only the ed25519 host key is kept.
- Root's password is **rotated to a random value stored in the vault** (§4.4) and kept only as the provider-console
  break-glass login. SSH login as root is disabled.

---

## 9. DNS stack configuration

### Unbound (`127.0.0.1:53` / `[::1]:53`, the host's resolver and AdGuard's only upstream)

`do-ip6` (auto), `prefetch: yes`, `prefetch-key: yes`, `serve-expired: yes` (+ `serve-expired-ttl: 86400`,
`serve-expired-client-timeout: 1800`), `qname-minimisation: yes`, `aggressive-nsec: yes`, `harden-glue`,
`harden-dnssec-stripped`, `harden-below-nxdomain`, `harden-referral-path: no` (too costly),
`harden-algo-downgrade`, `use-caps-for-id: no` (breaks some domains), `hide-identity`, `hide-version`, `hide-trustanchor`,
`private-address` for all RFC1918/ULA/link-local ranges (rebinding protection), `private-domain` for our own domain, no
`send-client-subnet` (no ECS), `edns-buffer-size: 1232`, `minimal-responses: yes`, `deny-any: yes`,
Debian's `auto-trust-anchor-file`, root hints from the `dns-root-data` package (updated through apt), cache sizes tuned for 1 GB RAM, `access-control`
limited to localhost. `/etc/resolv.conf` → `127.0.0.1` (Unbound directly, so the host doesn't depend on the AdGuard
container).

### AdGuard Home

- **Listen:** DNS on the **tunnel addresses only** (`10.8.0.1:53`, `<ula>::1:53`). `net.ipv4/ipv6.ip_nonlocal_bind=1` lets AdGuard start before `wg0` exists. This avoids any clash with Unbound on loopback. DoT `:853/tcp`, DoQ `:853/udp`, DoH through Caddy
  (`dns.vpn.*/dns-query/<ClientID>` → `127.0.0.1:3000`, with `allow_unencrypted_doh: true` + `trusted_proxies:
  127.0.0.1`). Web UI on `127.0.0.1:3000`.
- **Upstream:** only `127.0.0.1:53` (Unbound). No `fallback_dns`. `bootstrap_dns` points to Unbound. `upstream_mode: load_balance`.
- **Access:** `allowed_clients` = the VPN v4/v6 subnets plus the generated **ClientIDs** (one per device, such as
  `iphone-<rand>`). **Localhost is deliberately not allowed**: the host resolves through Unbound directly, and DoH
  requests arrive from Caddy on `127.0.0.1`, so allowing localhost would turn DoH into an open resolver if
  forwarded-IP handling ever broke. Unknown clients are refused.
- **Blocking:** `blocking_mode: nxdomain`, `enable_dnssec: true` (DO bit), `edns_client_subnet.enabled: false`,
  `ratelimit: 50`, `refuse_any: true`, `aaaa_disabled: false`, `cache` small (Unbound already caches).
- **Filter profiles** (chosen at deploy; default **Balanced**). Every list updates every 24h.

  | Profile | Lists |
  |---------|-------|
  | Minimal | AdGuard DNS filter, HaGeZi Threat Intelligence Feeds (mini) |
  | **Balanced** | AdGuard DNS filter, HaGeZi Multi **PRO**, HaGeZi Threat Intelligence Feeds |
  | Aggressive | AdGuard DNS filter, HaGeZi Multi **PRO++**, HaGeZi Threat Intelligence Feeds (full), HaGeZi Badware Hoster |

  Plus, in every profile when "block encrypted-DNS bypass" is on: HaGeZi **DoH/VPN/TOR/Proxy Bypass**.
- **Custom rules:**
  - `||use-application-dns.net^` (Firefox canary turns off Firefox's DoH)
  - `||mask.icloud.com^` and `||mask-h2.icloud.com^` (turns off iCloud Private Relay on the VPN; Apple falls back
    gracefully)
  - Allowlist entries for anything that breaks go in `config/adguard-allowlist.txt` (templated)
- **Rewrites** (local names):
  - Only the **UI hostnames** are rewritten: `auth.<ns>`, `wg.<ns>`, `adguard.<ns>` and `dns.<ns>` → the VPS's wg0
    address (v4 + v6), so UI traffic stays inside the tunnel.
  - The **WireGuard endpoint `<ns>.<zone>` is never rewritten.** If it resolved to the internal address, clients
    re-resolving while roaming or using on-demand/always-on modes would fail to reconnect.
  - Because the endpoint is excluded, there's no wildcard rewrite. Each UI hostname gets its own explicit rewrite.
- **Web auth:**
  - AdGuard keeps a **random admin password** (bcrypt in `AdGuardHome.yaml`, plaintext in the vault).
  - After Authelia approves a request, Caddy adds the matching `Authorization: Basic` header, so you still sign in only
    once through Authelia.
  - The UI listens on loopback only. The password protects AdGuard's admin API from other processes on the shared host
    network.
- **TLS for DoT/DoQ:** Caddy gets the cert for `dns.vpn.*`. A `vpn-certsync` path unit copies it to
  `/opt/vpn/adguard/certs/` (0640) and restarts AdGuard when it changes.

---

## 10. Auth (Authelia) & web front door (Caddy)

- **Authelia**
  - `users.yml`: one admin user (prompted email, used for the wg-easy OIDC identity), argon2id hash.
  - **2FA methods:**
    - TOTP is the default method.
    - **WebAuthn/passkeys** are an optional, phishing-resistant method (advanced toggle, default on) that you can enroll
      from the Authelia settings page.
    - Duo is off.
    - The TOTP issuer is the domain.
    - When identity is stored (§13), the TOTP secret is **pre-generated in the vault** and imported with
      `authelia storage user totp import`, so a rebuilt server accepts your existing authenticator entry.
  - **Access control:** `default_policy: deny`; `auth.*` bypass (the portal itself); `wg.*`, `adguard.*` →
    `two_factor`; `dns.*/dns-query*` → `bypass` (DoH clients can't do interactive auth; the ClientID allowlist protects
    it).
  - **Sessions:** cookie domain `vpn.<domain>`, `inactivity: 1h`, `expiration: 12h`, **`remember_me: 30d`** (you
    enabled it). Secure + HttpOnly + SameSite=Lax.
  - **Regulation:** **`modes: [ip]`**, `max_retries: 3`, `find_time: 10m`, `ban_time: 1h`.
    - IP mode bans the attacking address instead of your account, so a stranger who knows your username can't lock you
      out (Authelia's default `user` mode allows that, and its docs recommend `ip`).
    - fail2ban adds network-level bans after 5 failures in 10 minutes.
  - **Notifier:** filesystem (`/config/notification.txt`), used only for identity-verification codes, such as when
    you register a passkey. The TOTP secret is shown as a QR code by `deploy.sh` (§4.6). Codes are read with
    `ssh <ns> sudo vpn-status --auth-code`. Password reset is disabled.
  - **OIDC provider:** one client `wg-easy`, `token_endpoint_auth_method: client_secret_post`, `require_pkce: true`
    (S256), redirect URIs `https://wg.vpn.<domain>/api/auth/oidc/{callback,link}`, scopes `openid email profile`,
    `authorization_policy: two_factor`, `consent_mode: implicit`.
  - Storage: SQLite with an encryption key; JWT/session/HMAC secrets come from the vault and are passed as
    `*_FILE` secrets.
- **wg-easy SSO flow:** Caddy forward_auth makes sure you have an Authelia session. wg-easy then uses
  `OAUTH_AUTO_LAUNCH=oidc`, so the OIDC redirect goes straight back through Authelia with no extra prompt. Settings:
  `OAUTH_DISABLE_PASSWORD=true`, `OAUTH_AUTO_REGISTER=true`, and `OAUTH_ALLOWED_DOMAINS` = your email domain.
  - The account is created on the first unattended run (`INIT_USERNAME/PASSWORD`, with a random password). OIDC is
    linked through matching email.
  - **Setup order** (in the `wg_easy` role):
    1. First start with password login on.
    2. Apply the hooks change and settings (§2).
    3. Configure OIDC.
    4. Verify an OIDC login programmatically.
    5. Only then set `OAUTH_DISABLE_PASSWORD=true` and recreate the container.
  - **Real break-glass path** (in the README), because the password login is disabled:
    `ssh <ns>` → `sudo vpn-breakglass wg-easy` temporarily sets `OAUTH_DISABLE_PASSWORD=false` and recreates the
    container. It reverts on the next deploy or maintenance run.
- **Caddy**
  - TLS 1.2/1.3 only, automatic HTTPS (Let's Encrypt with ZeroSSL fallback), HTTP→HTTPS redirect, HTTP/3.
  - `--staging` switches the ACME CA to Let's Encrypt staging and turns off the ZeroSSL fallback.
  - Headers: `Strict-Transport-Security: max-age=63072000; includeSubDomains` (**no `preload`**, because only a
    registrable apex domain can be preloaded and preload is hard to undo), CSP for each app, `X-Frame-Options DENY`,
    `X-Content-Type-Options nosniff`, `Referrer-Policy no-referrer`, `Permissions-Policy` locked down, the `Server`
    header removed.
  - **AdGuard route:** `forward_auth` to Authelia, then `header_up Authorization "Basic <vault>"` to AdGuard.
  - **DoH route:** only `/dns-query/<ClientID>` (with a non-empty ClientID that matches `[a-z0-9-]+`) is proxied.
    Bare `/dns-query` and anything else on `dns.<ns>` return **404**. This is a second layer of protection on top of
    AdGuard's allowlist.
  - Requests for unknown hosts or a bare IP get a closed connection (`abort`).

---

## 11. Updates & self-healing

All times are in the server timezone, which is set to the timezone you chose. **T** is the maintenance time (default
02:00).

### 11.1 Nightly maintenance (one sequential run)

`vpn-maintenance.timer` (`OnCalendar=*-*-* T`, `Persistent=true`, `RandomizedDelaySec=0`) starts
`vpn-maintenance.service`. Its steps run **in order**, so one step can never overlap the next:

1. **OS packages:**
   - Runs `unattended-upgrade` directly. The stock `apt-daily-upgrade.timer` is disabled, so there's no second
     schedule. `apt-daily.timer` is kept only to refresh package lists.
   - Origins: Debian, Debian-security, Debian updates, Docker CE.
   - `Remove-Unused-Dependencies`, `AutoFixInterruptedDpkg`.
2. **Containers:**
   1. For each service, resolve the target tag (major tag, or the newest `v0.107.*` for AdGuard; §2) and pull it.
   2. If the new digest is in the **quarantine list** in `/opt/vpn/images.lock`, skip it and keep the current digest.
   3. Otherwise `up -d` with the new digest and wait up to 5 min for the healthcheck.
   4. **If unhealthy:** roll back to the previous digest in `images.lock`, **add the bad digest to quarantine**, and
      mark the run as failed. A quarantined digest is never retried. The next *different* digest the publisher
      releases is tried normally. This avoids the nightly break/rollback loop.
   5. If healthy, record the new digest in `images.lock`.
   6. Finally run `docker image prune -f`.
3. **Weekly (Sundays):** refresh Unbound root hints, and check for **new major versions** of each image (reported
   only, never applied automatically).
4. **Reboot if required** (toggle, default on): if `/var/run/reboot-required` exists, reboot at the end of the run.
5. **Healthchecks ping (optional):**
   - Success → `<url>`.
   - Any step failed → `<url>/fail`, with a short body naming the failed step or quarantined image.
   - Healthchecks.io emails you if no ping arrives within 26h.
   - The success ping is sent **before** the reboot, so a reboot doesn't look like a missed run.
6. `vpn-status` always shows the last run's result, the timestamp, any quarantined digests, and available major
   upgrades.

### 11.2 Self-healing

| Interval | What | How |
|----------|------|-----|
| every 2 min | Restart unhealthy containers | `vpn-heal.timer` restarts any container reporting `unhealthy`. This replaces an autoheal container, so no Docker socket is exposed. It doesn't run while `vpn-maintenance` is active (shared lock file) |

- **Boot:** `vpn-stack.service` (`Type=oneshot`, `RemainAfterExit`, `After=docker.service network-online.target
  nftables.service unbound.service`) runs `docker compose up -d`. Every service has `restart: unless-stopped`.
- **Docker:** `live-restore: true`, so containers keep running when dockerd itself is upgraded.
- **AmneziaWG (if enabled):** the DKMS module rebuilds automatically on kernel upgrades. If the build fails,
  `OVERRIDE_AUTO_AWG` is not set, so wg-easy falls back to standard WireGuard. Client compatibility then breaks, and
  this is flagged in `vpn-status`.

---

## 12. Verification (end of every deploy)

- Run locally from your machine:
  - TLS grade checks (`openssl s_client` TLS 1.3, HSTS present).
  - `wg.` and `adguard.` return 302 → `auth.` without a cookie.
  - DoH query with a valid ClientID → answer. **Negative tests:** bare `/dns-query` → 404, an unknown ClientID →
    refused, and plain DNS on :53 from the internet → no response.
  - With `--staging`, a warning is printed that the certificates are untrusted.
  - `nmap`-style port probe (via `nc`) confirms that only the expected ports are open.
- Run on the server:
  - All containers are healthy and `vpn-stack` is active after `systemctl restart docker`.
  - `dig @127.0.0.1 dnssec-failed.org` → SERVFAIL, and `sigok.verteiltesysteme.net` → NOERROR with `ad` flag.
  - AdGuard blocks a known ad domain.
  - `nft list ruleset` contains the forced-DNS DNAT.
  - journald `Storage=volatile`, `/var/log` is tmpfs, `wtmp`/`btmp`/`lastlog` → `/dev/null`, and no files exist
    under `/var/log/journal`.
  - From a VPN-side query (`dig @<wg0 addr>`), the UI hostnames resolve to wg0 and **`<ns>.<zone>` resolves to the
    public IP**.
  - `systemd-resolved` is inactive, and AdGuard is bound to :53.
  - Authelia regulation mode is `ip`. A request to AdGuard's API on loopback **without** auth → 401.
  - `sshd -T` matches policy.
  - unattended-upgrades dry-run passes.
  - `vpn-maintenance.timer` is scheduled at T local time, and the stock `apt-daily-upgrade.timer` is disabled.
  - `lynis audit system --quick` hardening index is printed.
- Manual checks (README): connect a device, then check dnsleaktest.com, ipleak.net (no v4/v6 leak), and
  `https://<adguard-test>`.

---

## 13. Backups & server identity (optional)

Two independent options. Both live in the advanced prompts (§4.3).

### 13.1 Server identity in the vault (default **on**)

The aim is to make a rebuilt VPS look the same to every client. These small values live in `config/vault.yml` on
your machine:

| Item | Effect on rebuild |
|------|-------------------|
| WireGuard server private key + ULA prefix + listen port | Client configs stay valid |
| Peer list (name, public key, preshared key, addresses, enabled flag) | Every device reconnects with no re-import |
| Authelia TOTP secret | Your authenticator app entry keeps working |

- **First deploy:** the server key and TOTP secret are generated **locally** by the wizard and pushed to the server.
- **Every deploy:** the `identity` role reads the current peer list from wg-easy's database and writes it back to
  the vault, so peers you added in the web UI are captured. The wizard shows a short diff ("+2 peers") before saving.
- **Rebuild:** wg-easy is initialised with the stored key, and the peers are inserted before first start. Authelia
  imports the stored TOTP secret.
- **Limitations:**
  - Peers added in the UI *after* the last deploy are not captured until you run `./deploy.sh` (or
    `./deploy.sh --tags identity`) again. The README says so.
  - Passkeys can't be exported, so you would re-register them after a rebuild.
  - AdGuard customisations made in the UI are not covered. They need full backups, or should be put in the templated
    allowlist and custom-rules files.

### 13.2 Full encrypted state backups (default **off**, opt-in)

- Running `./backup.sh` on your machine calls `sudo vpn-backup-export` over SSH. That briefly stops writes (a short
  container pause) and streams a tar of:
  - the wg-easy DB
  - AdGuard `conf/` and `work/` (filters excluded; they are re-downloaded)
  - Authelia's DB (after the auth-log purge)
  - `images.lock`
- The stream is encrypted **on the server** with [age](https://age-encryption.org) to a public key whose private key
  lives only in your vault. It is saved as `backups/<ns>-<timestamp>.tar.age` (gitignored). Nothing is stored on the
  VPS.
- `deploy.sh` runs a backup at the end of each deploy when this is on, and keeps the last 10.
- **Restore:** on a fresh VPS, the wizard offers to restore the newest backup (§4.6). It is decrypted locally,
  uploaded, and unpacked before the stack's first start.
- Full backups are a superset of 13.1. When both are on, the backup's state takes precedence and the vault identity
  is checked for consistency.

---

## 14. Testing strategy

- **Static checks:**
  - Run with `pre-commit` locally and in GitHub Actions on every push: `ansible-lint` (production profile),
    `yamllint`, `shellcheck` and `shfmt` for shell scripts, and `ruff` + `pytest` for the wizard.
  - Wizard unit tests cover NS-to-provider detection, timezone detection, Cloudflare change-table building (with a
    mocked API), and settings round-trips.
- **Idempotence:** every live test runs the playbook twice. The second run must report `changed=0`.
- **Live test matrix** (run on a fresh VPS for each phase, §15):
  1. Fresh install (password path and existing-key path).
  2. Re-run (idempotence).
  3. `reboot`, then everything is healthy within 2 min.
  4. `docker kill` each container, then it is healed within 2 min.
  5. `systemctl restart docker`, then the stack recovers.
  6. A forced `vpn-maintenance` run with a deliberately bad image digest, then rollback + quarantine + `/fail` ping.
  7. A rebuild with identity on, then an existing client reconnects without changes.
- **Client checklist** (README):
  - Connect from a phone and a laptop.
  - Check dnsleaktest.com (extended) and ipleak.net, with no v4 or v6 leak.
  - Confirm ads are blocked, hardcoded `8.8.8.8` lookups are intercepted, and DoT to a public resolver is blocked.
  - Confirm `wg.`/`adguard.` force a login, and TOTP and passkey login work.
- Use `--staging` for every test run until the final full-run test.

---

## 15. Implementation roadmap (phased delivery)

These phases are about **how we build and test** the code. They are not how a user installs it. The finished
`./deploy.sh` always installs everything in one run. Each phase ends with a live test on a freshly wiped VPS (§14)
before the next one starts. The playbook tags match the phases, so they stay useful later (`--tags`).

| Phase | Scope | Exit criteria |
|-------|-------|---------------|
| **(a) Core VPN** | Wizard skeleton (essential prompts, vault, keychain), SSH bootstrap + lockdown, `base`, `ssh`, `logging`, `firewall`, `fail2ban`, `docker`, `unbound`, `adguard`, `wg_easy` (password login, temporary SSH-tunnel access to the UI), `stack`, the hooks spike from §2 | A phone connects. Ads are blocked. No DNS or IPv6 leaks. Survives a reboot. Idempotent. No logs on disk |
| **(b) Front door** | `caddy`, `authelia` (TOTP, IP regulation, optional WebAuthn), OIDC SSO for wg-easy + disabling password login, AdGuard header injection, DNS records printed + DNS gate, `--staging` | Certificates are issued. Unauthenticated requests redirect to auth. SSO lands in wg-easy logged in. The AdGuard API rejects unauthenticated loopback requests |
| **(c) Operations** | `maintenance` (sequential run, digest lock, quarantine), `vpn-heal`, Healthchecks, `tools` (`vpn-status`, `vpn-debug`, `vpn-breakglass`), zram + memory limits, `verify` role | Test-matrix items 3–6 pass. The Healthchecks success and fail pings arrive |
| **(d) Extras** | Public DoH/DoT/DoQ with ClientIDs + cert sync, Cloudflare API automation, provider detection, advanced prompts, identity-in-vault, full backups/restore, AmneziaWG opt-in, `--non-interactive` | Test-matrix item 7 passes. Each toggle works on and off. Final **full single-run install** on a wiped VPS without `--staging` |

---

## 16. Decision log

| Q | Topic | Decision |
|---|-------|----------|
| 1 | Admin UI exposure | Public, with every UI behind Authelia 2FA |
| 2 | DNS/certs | DNS provider is detected from NS and you confirm it; records are DNS-only; HTTP-01 certificate for each host; no DNS token ever on the VPS |
| 3 | DNS filter | AdGuard Home + Unbound; blocklist profile prompted (default **Balanced**) |
| 4 | Auth | Authelia, TOTP default, no SMTP. *Revised by R13:* AdGuard keeps a vault password injected by Caddy, so you still sign in once. *Revised by R12:* optional passkeys |
| 5 | Tooling/runtime | Ansible + `deploy.sh`; Docker + Compose (Unbound via apt is the one exception, see §2) |
| 6 | Updates | Major-version tracking + rollback, auto-reboot when required (advanced toggle). *Revised by R6/R7/R14/R15:* sequential run, digest lock + quarantine, AdGuard pinned to v0.107.x, Authelia on `:4` |
| 7 | Logging | RAM-only journald (1h/16 MB), fail2ban + nftables rate-limits, `vpn-debug` that reverts on reboot, AdGuard aggregate stats kept (24h) |
| 8 | SSH/user | sudo requires a password, port 22 by default (advanced) |
| 9 | VPN network | UDP 51820, full tunnel, client isolation on, IPv6 auto (NAT66 if the VPS has v6, otherwise blackhole), AmneziaWG opt-in (off) |
| 10 | DNS extras | Baseline + bypass blocking + Private Relay block + public DoH/DoT/DoQ with ClientID allowlist + local rewrites (each one an advanced toggle) |
| 11 | Backups/alerts | *Revised by B1:* backups are optional (see B1). Healthchecks.io dead-man's switch is **optional** (prompted, default off) |
| 12 | Hardening | Defaults accepted; Authelia remember-me **on** (30d) |
| U1 | Prompt style | Essentials are always asked, then "use recommended defaults for advanced? [Y/n]". Answers are saved and pre-filled on re-runs |
| U2 | SSH start state | Ask whether a key is already on the VPS (pick key + login user) or install one via the root password (existing or newly generated key). Host key pinned. Root login **always** disabled; password login disable is asked (strongly recommended) |
| U3 | Domain | Generic `<zone>` prompt (subdomain zones allowed), namespace label default `vpn`, labels renamable; provider detected from NS, confirmed or overridden |
| U4 | Cloudflare API | Optional token (Zone:DNS:Edit, one zone), verified, kept in the local vault only; change table + confirmation before any write; never deletes; DNSSEC + CAA stay manual |
| U5 | Timezone | Defaults to this computer's timezone; maintenance time configurable (advanced, default 02:00) |
| U6 | Prompt list | §4.2 essentials / §4.3 advanced as listed; blocklist default for new users = Balanced |
| R1 | Endpoint rewrite | The WireGuard endpoint is never rewritten. Only UI hostnames are rewritten to wg0 |
| R2 | SSH rate limit vs Ansible | Pipelining + ControlPersist; limit 20/min burst 10; timed deployer-IP exemption + fail2ban `ignoreip` |
| R3 | Logs on disk | `/var/log` on tmpfs; wtmp/btmp/lastlog → /dev/null; Authelia auth-log purge every 15 min |
| R4 | Authelia lockout | Regulation `modes: [ip]` |
| R5 | DoH open resolver | Localhost removed from `allowed_clients`; Caddy requires a ClientID path (otherwise 404); negative tests |
| R6 | AdGuard `latest` | Track the newest `v0.107.*`, resolved to a digest |
| R7 | Rollback flapping | `images.lock` digest pinning + quarantine of failed digests |
| R8 | wg-easy hooks order | Set hooks before disabling password login; fallback: coexist with wg-easy's own rules |
| R9 | Port 53 / host-network settings | Disable and mask systemd-resolved; forwarding settings on the host, none in compose |
| R10 | LE rate limits | `--staging` flag |
| R11 / B1 | Backups | Optional, two levels: server identity in the vault (default **on**), full age-encrypted state backups (default **off**) — §13 |
| R12 | Phishing-resistant 2FA | Optional WebAuthn/passkeys (default on); TOTP stays the default |
| R13 | AdGuard admin API | Random password, header injected by Caddy after Authelia |
| R14 | Overlapping schedules | A single sequential `vpn-maintenance` run replaces the fixed T/T+15m/T+45m offsets |
| R15 | Stale minors | Authelia tracks `:4`; weekly check reports new majors |
| R16 | Memory | zram, container memory limits, conservative Unbound cache; README recommends 2 GB for Aggressive |
| R17 | Wizard language | Python CLI via uv; `deploy.sh` is a thin launcher |
| R18 | Root password | Rotated to a random value in the vault |
| R19 | Small items | `/tmp` executable; HSTS without preload; `vpn-breakglass` documented; 853 exemption for own IP; `--non-interactive` |
| R20 | Delivery | Phased build/test roadmap (§15). Users still install in one run |
| R21 | Testing | Lint/CI, idempotence, live test matrix (§14) |
| R22 | Threat model | Documented (§1.1) |

---

## 17. Repository layout

```
.
├── README.md                 # User-facing setup guide
├── PLAN.md                   # This document
├── deploy.sh                 # Thin launcher → wizard
├── backup.sh                 # Optional full-state backup (§13.2)
├── status.sh                 # Remote status helper
├── pyproject.toml / uv.lock  # Wizard deps + pinned ansible-core
├── wizard/                   # Python CLI: prompts, vault, keychain, NS detection, Cloudflare, SSH bootstrap
├── tests/                    # pytest for the wizard
├── ansible/
│   ├── ansible.cfg           # pipelining, ControlPersist
│   ├── requirements.yml      # Collections
│   ├── bootstrap.yml         # First-run admin user + lockdown
│   ├── site.yml              # Main playbook (tags = roles/phases)
│   ├── group_vars/all.yml    # Defaults, image tracks, blocklist profiles
│   └── roles/<role>/{tasks,templates,files,handlers,defaults}/
├── .pre-commit-config.yaml   # ansible-lint, yamllint, shellcheck, ruff
├── .github/workflows/ci.yml  # Same checks in CI
├── config/                   # gitignored: settings.yml, vault.yml (encrypted), generated inventory
├── backups/                  # gitignored: *.tar.age
└── .gitignore
```

---

## 18. Open questions / risks to confirm during implementation & live testing

1. ~~**wg-easy hooks override**~~ **Resolved in Phase (a).** See §19.
2. **wg-easy identity restore:** wg-easy has no documented way to import a server key or peers at first start. The
   plan is to seed its SQLite database before first start. The schema may change between v15 minors, so the
   `identity` role checks the schema version and fails loudly (rather than corrupting data) if it's unknown.
3. **wg-easy + Authelia OIDC:** relies on email matching between the INIT user and the Authelia user. Tested live in
   Phase (b). The break-glass path is `vpn-breakglass wg-easy`.
4. **Authelia TOTP import:** confirm that `authelia storage user totp import` (or `generate --secret`) in 4.39+
   accepts a pre-generated secret. Fallback: enroll through the UI and export the secret back to the vault with
   `authelia storage user totp export`.
5. **Authelia `:4` tag:** confirm the major tag is published and updated on every release. If it isn't, track
   `4.x` with the same digest and quarantine logic used for AdGuard.
6. **AdGuard password via header injection:** confirm the AdGuard UI accepts Basic auth on every API call without
   showing its own login page. Fallback: AdGuard's session cookie, obtained by Caddy with a one-time login
   handshake (more complex).
7. **Private Relay block:** Apple shows a "Private Relay unavailable" notice on that network. This is expected.
8. **Aggressive lists:** expect occasional breakage. The allowlist file plus re-deploying (or the AdGuard UI) handles
   it.
9. **Provider quirks:**
   - Some VPS images ship with cloud-init SSH drop-ins (e.g. `50-cloud-init.conf` setting
     `PasswordAuthentication yes`). The `ssh` role removes or overrides them.
   - Some providers' default images lack IPv6 until you enable it in their panel.
   - Some ship `systemd-resolved` enabled, which is handled by R9.
10. **NS-based provider detection** is a best guess. Resellers and white-label DNS can be misidentified, so the user
    always confirms or overrides it. If it's wrong, the only effect is that the API option is or isn't offered.
11. **Timezone detection** reads the `/etc/localtime` symlink. Some Linux setups use a copied file instead of a
    symlink, so the script falls back to `timedatectl` and then to UTC, and always shows the value so you can override
    it.
12. **`/var/log` on tmpfs:** a few packages expect subdirectories (e.g. `/var/log/apt`) to exist at boot. A
    `tmpfiles.d` entry recreates them. Verified in Phase (a).
13. **Deployer-IP detection:** the wizard learns your public IP from the server's view (`$SSH_CLIENT`), not from a
    third-party "what's my IP" service, so nothing is leaked.

---

## 19. Implementation notes (Phase a)

These are what we learned while building and testing Phase (a) in the local VM (`tests/vm/`). Where they differ from
earlier sections, these notes are authoritative.

**Test harness**
- `tests/vm/test-vm.sh` creates a Lima Debian 13 VM that behaves like a freshly imaged VPS (root + password SSH,
  cloud-init, `systemd-resolved` on :53).
- `tests/vm/deploy-vm.sh [--fresh]` runs the real `./deploy.sh --non-interactive` against it, using a separate test
  config directory (`VPN_CONFIG_DIR`) so your real config and keychain are never touched.
- `tests/vm/client.yaml` + `client-checks.sh` connect a second VM as a WireGuard client and run 13 end-to-end checks:
  - handshake and internet through the tunnel
  - DNS through AdGuard, with ads blocked
  - forced DNS (queries to `8.8.8.8` are intercepted)
  - DoT bypass blocked
  - admin UIs not exposed on the tunnel
  - IPv6 routed into the tunnel and refused (no leak)
  - SSH still reachable through the tunnel

**Validated (local VM, then the live VPS 203.0.113.10 with 2 GB RAM, x86_64, IPv4 only):**
- A fresh install (root-password path) completes in about 8 minutes.
- A re-run reports `changed=0`.
- The stack survives a reboot.
- Containers recover after `kill -9`.
- Drift is repaired: cloud-init re-adding a password drop-in and RSA/ECDSA host keys is undone on the next deploy.
- Root and password SSH logins are refused, and only the ed25519 host key is offered.
- All 13 client checks pass.
- **Real device (live VPS):** an iPhone connected over the WireGuard app. ipleak.net showed only the VPS IP and no
  IPv6, the dnsleaktest.com extended test showed only the VPS (no ISP or carrier DNS), and the d3ward adblock test
  scored 99%.

**Design changes and findings**
1. **wg-easy hooks (§18 risk 1, resolved).**
   - wg-easy keeps its settings in SQLite (`/etc/wireguard/wg-easy.db`) and its image uses **iptables-legacy**. Its
     default hooks would add rules that are invisible to `nft list ruleset` (they live in legacy iptables, outside
     nftables) but still take effect, and they assume `eth0`.
   - The `wg_easy` role enforces the following on every run:
     - empty hooks
     - the real public interface, port, endpoint, DNS, and keepalive
   - These are applied with a graceful `docker compose stop` (its PostDown removes its rules), then an SQLite update,
     then a start. A check fails the deploy if any legacy rule is left behind.
   - wg-easy's API accepts HTTP Basic auth, which is useful for automation.
   - **Found on the live x86 VPS (not on the arm64 test VM):** wg-easy's built-in hooks run on its very first start,
     before we can empty them. They need the legacy `ip6table_nat` kernel module, and the container (deliberately
     without `SYS_MODULE`) can't load it, so wg-easy crash-looped. Each restart also left behind legacy IPv4 rules.
   - The role now loads the legacy NAT/filter modules while the built-in hooks are still set. If any legacy rules
     exist, it flushes the legacy tables (only wg-easy ever writes there) and verifies they're empty. It then unloads
     the modules.
   - The leftover check now fails on *any* legacy rule, not only ones mentioning wg0.
2. **DNS ports.**
   - Unbound serves `127.0.0.1:53` and `::1:53` (the host's resolver and AdGuard's upstream).
   - AdGuard binds only to the tunnel addresses, using `ip_nonlocal_bind`.
   - `systemd-resolved` is masked only after Unbound has been proven to resolve with DNSSEC, so apt never loses DNS.
3. **fail2ban runs in `normal` mode, not `aggressive`.**
   - Aggressive mode counted the wizard's own `ssh-keyscan` and probe connections, which close before
     authenticating, and banned the deploying machine.
   - Its log goes to the RAM journal and its ban database is `:memory:`. Your deploying IP is added to `ignoreip` at
     runtime only, so it is never written to disk.
4. **More log sources silenced:** cloud-init (logging override → NullHandler) and fail2ban's own log file. On first
   deploy, old logs on disk are wiped before `/var/log` becomes tmpfs.
5. **AdGuard memory.**
   - About 240–260 MiB with the Balanced lists. The limits now scale with the profile (192 / 384 / 512 MiB), and
     `GOMEMLIMIT` is set to 80% of the limit.
   - zram keeps a 1 GB VPS usable. Recommend 2 GB for Aggressive.
6. **AdGuard idempotence.** AdGuard rewrites its YAML on start (it removes comments and re-quotes strings), so the
   template mirrors its exact output and has no header comment.
7. **wg-easy runs without `SYS_MODULE`.** The host loads the `wireguard` module at boot. The container has `cap_drop:
   ALL` plus `NET_ADMIN` and `NET_RAW`. AdGuard has `cap_drop: ALL` plus `NET_BIND_SERVICE` and a read-only root
   filesystem.
8. **`vpn-stack.service` uses `Wants=docker`, not `Requires=`,** so a Docker upgrade doesn't stop the stack, which
   would defeat live-restore.
9. **IPv6 + forwarding.** `accept_ra=2` is set so that turning on IPv6 forwarding doesn't silently drop SLAAC
   addresses on VPS providers.
10. **Host key changes.** If the server's host key changed but your admin key still logs in, the wizard re-pins the
    key (after confirmation) and carries on, instead of starting a full re-bootstrap.
11. **SSH config.** If `~/.ssh/config` already has your own `Host <alias>` entry, the wizard offers to replace it
    (backup in `~/.ssh/config.bak`), because SSH uses the first matching entry.
12. **Prompt order.** SSH access is asked after the domain, because the name of a newly generated key includes the
    domain.
13. **Phase (a) web UI access** is through an SSH tunnel (`ssh -L 51821:…` / `-L 3000:…`). In Phase (b), Caddy and
    Authelia take over.
14. **Code review fixes (PR #1).** All were verified in the VM, and the dry-run and re-run checks also on the live VPS:
    - **Changing the sudo password** is safe to interrupt. The new password is stored as
      `vault_sudo_password_pending`. Before the playbook runs, the wizard sets it on the server with `chpasswd`,
      authenticating with the current password, and confirms sudo works with the new one. Only then does it become
      current. If anything fails, the old password still works. The `ssh` role also enforces the admin password from
      the vault.
    - **Changing the SSH port** works in one run. The wizard copies the already-trusted host key to `[IP]:<new port>`
      before the play, and the ssh role re-applies the firewall after switching, so the old port closes in the same
      run. The active port is found by testing logins after the play, never inferred from settings, so `--tags`
      runs and rollbacks can't record a wrong port.
    - **`--check`** never bootstraps (it exits with "nothing was changed" if the server isn't set up yet), never
      writes `~/.ssh/config` or settings, and read-only probes use `check_mode: false` so a dry run completes.
    - **Swap on disk** is turned off, removed from `/etc/fstab`, and deleted if it's a file. zram is started first.
    - **Turning fail2ban off** stops and disables it and removes its nftables table (lifting bans). The package
      stays installed.
    - The SSH apply steps live in `ssh/tasks/apply.yml` (a dynamic include with `apply: tags`), so the
      `reset_connection` meta task no longer ignores a block `when`.

---

## 20. Phase (b) decisions (2026-11-01)

| # | Topic | Decision |
|---|-------|----------|
| B-1 | Prompts for features not built yet (maintenance time, auto-reboot, public DoH, AmneziaWG, backups, Healthchecks) | **Hidden** until their phase ships. Their defaults stay in `settings.py` |
| B-2 | First live Phase (b) deploy | **You** run `./deploy.sh` interactively (choose your own web login password, scan the TOTP QR code). The agent handles the VM work, re-runs and debugging |
| B-3 | Authelia sign-in name | Admin username (e.g. `alice`). The email is used only as the OIDC identity for wg-easy |
| B-4 | Passkeys | On, as an optional second factor. TOTP remains the default |
| B-5 | Delivery | `phase-b` branch, then a PR |
| B-6 | Test TLS | VM: Caddy's internal CA (`tls_mode: internal`, test-only), with that root trusted by wg-easy (`NODE_EXTRA_CA_CERTS`) so OIDC works. Live: real Let's Encrypt. `--staging` mounts the LE staging roots into wg-easy the same way |
| B-7 | TOTP | Secret generated locally, stored in the vault, imported with `authelia storage user totp generate --secret`, and shown as a terminal QR code on first deploy, so it survives rebuilds |

