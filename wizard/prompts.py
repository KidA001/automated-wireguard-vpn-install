"""The interactive questions: essentials (always) and advanced (optional walk-through)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from wizard import detect, sshboot, ui
from wizard.secrets_store import random_password
from wizard.settings import ADVANCED_KEYS, DEFAULTS, hostnames

BLOCKLISTS = [
    ("minimal", "Minimal    – AdGuard DNS filter + malware/phishing feeds (lowest breakage)"),
    ("balanced", "Balanced   – + HaGeZi Multi PRO (recommended)"),
    ("aggressive", "Aggressive – + HaGeZi PRO++ and badware hosters (more blocking, occasional breakage)"),
]


def _keep_secret(vault: dict[str, Any], key: str, what: str) -> bool:
    return bool(vault.get(key)) and ui.confirm(f"Keep the existing {what}?", default=True)


def ssh_access(settings: dict[str, Any], ns: str, zone: str) -> dict[str, Any]:
    """Ask how we can reach the fresh VPS. Returns the bootstrap login plan."""
    ui.heading("SSH access")
    if ui.NON_INTERACTIVE:
        login_user = os.environ.get("VPN_BOOTSTRAP_USER", "root")
        return {"user": login_user, "password": os.environ.get("VPN_ROOT_PASSWORD") or None}

    key_on_vps = ui.confirm("Is your SSH public key already authorized on the VPS?", default=False)
    keys = sshboot.local_keys()
    if key_on_vps:
        settings["ssh_key_path"] = str(_choose_existing_key(keys, settings.get("ssh_key_path", "")))
        user = ui.text(
            "Which user does that key log in as?", default="root", validate=detect.v_username_or_root
        )
        return {"user": user, "password": None}

    choices = [("new", "Generate a new dedicated key (recommended)")]
    if keys:
        choices.append(("existing", "Use one of my existing keys"))
    how = ui.select("Which SSH key should the server trust?", choices, default="new")
    if how == "existing":
        settings["ssh_key_path"] = str(_choose_existing_key(keys, settings.get("ssh_key_path", "")))
    else:
        slug = "_".join(p for p in [ns, zone.replace(".", "_")] if p)
        path = sshboot.SSH_DIR / f"id_ed25519_{slug}"
        if path.exists():
            ui.info(f"Reusing existing key {path}")
        else:
            passphrase = ui.secret(
                "Passphrase for the new key (empty = none; on macOS it's saved in your keychain)",
                confirm=True,
                allow_empty=True,
            )
            sshboot.generate_key(path, f"{settings['admin_user']}@{zone}", passphrase)
            ui.success(f"Created {path}")
        settings["ssh_key_path"] = str(path)
    password = ui.secret("Root password for the VPS (used once, never stored)")
    return {"user": "root", "password": password}


def _choose_existing_key(keys: list[Path], current: str) -> Path:
    if keys:
        options = [(str(k), str(k).replace(str(Path.home()), "~")) for k in keys]
        options.append(("other", "Another path…"))
        default = current if current in {str(k) for k in keys} else str(keys[0])
        picked = ui.select("Which private key?", options, default=default)
        if picked != "other":
            return Path(picked)
    path = Path(ui.text("Path to the private key", default=current or str(sshboot.SSH_DIR / "id_ed25519")))
    path = path.expanduser()
    if not path.is_file():
        raise SystemExit(f"{path} doesn't exist.")
    return path


def essentials(settings: dict[str, Any], vault: dict[str, Any], need_ssh: bool) -> dict[str, Any] | None:
    ui.heading("Server")
    settings["vps_ipv4"] = ui.text("VPS public IPv4 address", settings["vps_ipv4"], detect.v_ipv4)

    ui.heading("Admin user")
    settings["admin_user"] = ui.text(
        "Admin username on the server",
        settings["admin_user"] or detect.local_username(),
        detect.v_username,
        help_text="A non-root user with sudo. Root SSH login is always disabled.",
    )
    if not _keep_secret(vault, "vault_sudo_password", "sudo password"):
        if ui.confirm("Generate a strong sudo password (stored in your encrypted vault)?", default=True):
            new_password = random_password(24)
        else:
            new_password = ui.secret("Sudo password", confirm=True, min_length=12)
        # An existing server still expects the current password; the wizard switches it over
        # safely before running the playbook (cli.apply_pending_sudo_password).
        key = "vault_sudo_password_pending" if vault.get("vault_sudo_password") else "vault_sudo_password"
        vault[key] = new_password

    ui.heading("Domain")
    zone = ui.text(
        "Your domain (the zone whose DNS you manage)",
        settings["zone"],
        detect.v_domain,
        help_text="e.g. example.com, or a delegated subdomain zone like home.example.net",
    ).lower()
    if zone != settings["zone"]:
        settings["dns_provider"] = ""
    settings["zone"] = zone
    _provider(settings)
    settings["vpn_ns"] = ui.text(
        "VPN namespace label (empty = put hosts directly under the domain)",
        settings["vpn_ns"],
        lambda v: detect.v_label(v, allow_empty=True),
    )
    hosts = hostnames(settings)
    ui.note(
        f"Endpoint {hosts['vpn']} · login {hosts['auth']} · WireGuard UI {hosts['wg']} · "
        f"AdGuard {hosts['adguard']} · encrypted DNS {hosts['dns']}"
    )

    login_plan = ssh_access(settings, settings["vpn_ns"], zone) if need_ssh else None

    ui.heading("SSH hardening")
    disable = ui.confirm(
        "Disable SSH password login? (strongly recommended)",
        default=settings["disable_password_auth"],
        help_text="Only your SSH key will be able to log in. Verified before it's switched off.",
    )
    if not disable:
        ui.warn(
            "Password logins are a common attack target. fail2ban will be stricter, but keys are much safer."
        )
        disable = not ui.confirm("Keep password login enabled anyway?", default=False)
    settings["disable_password_auth"] = disable

    ui.heading("Web login")
    settings["admin_email"] = ui.text(
        "Admin email (your login identity; no mail is sent)", settings["admin_email"], detect.v_email
    )
    if not _keep_secret(vault, "vault_authelia_password", "web login password"):
        vault["vault_authelia_password"] = ui.secret("Web login password", confirm=True, min_length=12)

    ui.heading("Schedule")
    tz_default, detected = detect.timezone()
    settings["timezone"] = ui.text(
        "Timezone",
        settings["timezone"] or tz_default,
        detect.v_timezone,
        help_text=(
            f"Detected from this computer: {tz_default}" if detected else "Couldn't detect; using UTC."
        )
        + " Nightly maintenance runs at 02:00 in this timezone.",
    )

    ui.heading("Ad blocking")
    settings["blocklist_profile"] = ui.select(
        "Blocklist profile", BLOCKLISTS, default=settings["blocklist_profile"]
    )

    ui.heading("Alerts")
    settings["healthchecks_enabled"] = ui.confirm(
        "Email me if the nightly update fails? (free Healthchecks.io dead-man's switch)",
        default=settings["healthchecks_enabled"],
        help_text="Healthchecks.io emails you if the server stops checking in after its nightly maintenance.",
    )
    if settings["healthchecks_enabled"] and not _keep_secret(vault, "vault_healthchecks_url", "ping URL"):
        ui.note(
            "Sign up at https://healthchecks.io, create a check (period 1 day, grace 2 hours), copy its URL."
        )
        vault["vault_healthchecks_url"] = ui.text(
            "Healthchecks ping URL",
            "",
            lambda v: v.startswith("https://") or "Paste the https:// ping URL.",
        )
    return login_plan


def _provider(settings: dict[str, Any]) -> None:
    if settings["dns_provider"] and ui.NON_INTERACTIVE:
        return
    ns_hosts = detect.nameservers(settings["zone"])
    guess = detect.provider_for(ns_hosts)
    current = settings["dns_provider"] or guess
    if ns_hosts:
        ui.note("Nameservers: " + ", ".join(ns_hosts))
    if current and ui.confirm(f"DNS provider: {current} — correct?", default=True):
        settings["dns_provider"] = current
        return
    options = [(name, name) for name in detect.PROVIDER_NAMES] + [("Other", "Other / not listed")]
    picked = ui.select("Who hosts your DNS?", options, default=current or "Other")
    if picked == "Other":
        picked = ui.text("Provider name", "Other")
    settings["dns_provider"] = picked


def advanced(settings: dict[str, Any]) -> None:
    custom = [k for k in ADVANCED_KEYS if settings[k] != DEFAULTS[k]]
    ui.heading("Advanced settings")
    if custom:
        ui.note("Customised: " + ", ".join(custom))
    if not ui.confirm("Review advanced settings?", default=False):
        return
    if ui.confirm("Reset all advanced settings to the recommended defaults first?", default=False):
        for key in ADVANCED_KEYS:
            settings[key] = DEFAULTS[key] if key != "labels" else dict(DEFAULTS["labels"])

    s = settings
    s["ssh_port"] = int(ui.text("SSH port", str(s["ssh_port"]), detect.v_port))
    s["wg_port"] = int(ui.text("WireGuard UDP port", str(s["wg_port"]), detect.v_port))
    s["maintenance_time"] = ui.text(
        "Nightly maintenance time (HH:MM, server timezone)", s["maintenance_time"], detect.v_time
    )
    s["auto_reboot"] = ui.confirm(
        "Reboot automatically after updates when required?", s["auto_reboot"],
        help_text="Only when a kernel/libc update needs it, at the end of the maintenance run.",
    )  # fmt: skip
    for key, purpose in [
        ("auth", "login"),
        ("wg", "WireGuard UI"),
        ("adguard", "AdGuard UI"),
        ("dns", "DoH"),
    ]:
        s["labels"][key] = ui.text(f"Subdomain label for the {purpose}", s["labels"][key], detect.v_label)
    s["ipv6_mode"] = ui.select(
        "IPv6 for VPN clients",
        [
            ("auto", "Auto – NAT66 if the VPS has public IPv6, otherwise block (no leaks either way)"),
            ("nat66", "Always NAT66"),
            ("blackhole", "Always block IPv6 inside the tunnel"),
        ],
        s["ipv6_mode"],
    )
    s["client_isolation"] = ui.confirm(
        "Isolate VPN devices from each other?", s["client_isolation"],
        help_text="Devices can reach the internet and the VPN's DNS, but not each other.",
    )  # fmt: skip
    s["amnezia"] = ui.confirm(
        "Enable AmneziaWG obfuscation? (all devices must use the AmneziaWG app)", s["amnezia"],
        help_text="Only useful on networks that block VPNs with deep packet inspection.",
    )  # fmt: skip
    s["public_doh"] = ui.confirm(
        "Offer encrypted DNS (DoH/DoT/DoQ) for devices off the VPN?", s["public_doh"],
        help_text="Ad blocking on your phone without the tunnel. Only answers secret per-device IDs.",
    )  # fmt: skip
    s["block_private_relay"] = ui.confirm("Block iCloud Private Relay on the VPN?", s["block_private_relay"])
    s["block_dns_bypass"] = ui.confirm(
        "Block apps from bypassing the VPN's DNS (DoH/DoT bypass lists)?", s["block_dns_bypass"]
    )
    s["adguard_stats"] = ui.confirm(
        "Keep AdGuard aggregate statistics (24h, no per-device query log)?", s["adguard_stats"]
    )
    s["remember_me"] = ui.text(
        "Web login 'remember me' duration (0 = off)", s["remember_me"], detect.v_duration
    )
    s["passkeys"] = ui.confirm("Allow passkeys / security keys as a second factor?", s["passkeys"])
    s["identity_in_vault"] = ui.confirm(
        "Keep the server identity (WireGuard key, devices, TOTP secret) in your vault?",
        s["identity_in_vault"],
        help_text="A rebuilt VPS then needs no changes on your devices.",
    )
    s["full_backups"] = ui.confirm("Enable full encrypted state backups (./backup.sh)?", s["full_backups"])
    s["fail2ban_enabled"] = ui.confirm("Enable fail2ban?", s["fail2ban_enabled"])
