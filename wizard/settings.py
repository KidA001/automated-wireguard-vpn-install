"""Locations and persistence of non-secret settings (config/settings.yml)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent
# VPN_CONFIG_DIR is for tests (tests/vm) so they never touch your real config.
CONFIG_DIR = Path(os.environ.get("VPN_CONFIG_DIR", REPO / "config")).resolve()
SETTINGS_FILE = CONFIG_DIR / "settings.yml"
VAULT_FILE = CONFIG_DIR / "vault.yml"
INVENTORY_FILE = CONFIG_DIR / "inventory.yml"
ANSIBLE_DIR = REPO / "ansible"
COLLECTIONS_DIR = REPO / ".ansible" / "collections"

# Every setting the wizard manages, with its recommended default. Keys match the
# Ansible variable names in ansible/group_vars/all.yml.
DEFAULTS: dict[str, Any] = {
    # essentials
    "vps_ipv4": "",
    "admin_user": "",
    "ssh_key_path": "",
    "disable_password_auth": True,
    "zone": "",
    "dns_provider": "",
    "vpn_ns": "vpn",
    "timezone": "",
    "blocklist_profile": "balanced",
    "healthchecks_enabled": False,
    # advanced
    "ssh_port": 22,
    "wg_port": 51820,
    "maintenance_time": "02:00",
    "auto_reboot": True,
    "labels": {"auth": "auth", "wg": "wg", "adguard": "adguard"},
    "ipv6_mode": "auto",
    "client_isolation": True,
    "block_private_relay": True,
    "block_dns_bypass": True,
    "adguard_stats": True,
    "remember_me": "30d",
    "passkeys": True,
    "identity_in_vault": True,
    "full_backups": False,
    "fail2ban_enabled": True,
    # internal state (not prompted)
    "ssh_port_active": 22,
    "wg_ipv6_cidr": "",
    "bootstrapped": False,
    "totp_shown": False,
}

# Settings offered in the advanced walk-through.
ADVANCED_KEYS = [
    "ssh_port",
    "wg_port",
    "maintenance_time",
    "auto_reboot",
    "labels",
    "ipv6_mode",
    "client_isolation",
    "block_private_relay",
    "block_dns_bypass",
    "adguard_stats",
    "remember_me",
    "passkeys",
    "identity_in_vault",
    "full_backups",
    "fail2ban_enabled",
]


def load() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        data = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    merged = {**DEFAULTS, **data}
    stored = {k: v for k, v in (data.get("labels") or {}).items() if k in DEFAULTS["labels"]}
    merged["labels"] = {**DEFAULTS["labels"], **stored}
    if merged.get("wg_ipv6_cidr"):
        from wizard.detect import canonical_cidr

        merged["wg_ipv6_cidr"] = canonical_cidr(merged["wg_ipv6_cidr"])
    for gone in ("amnezia", "public_doh"):  # removed settings (PLAN.md §23)
        merged.pop(gone, None)
    # The account email is generated on the server now; drop leftovers that were never set.
    if not merged.get("admin_email"):
        merged.pop("admin_email", None)
    return merged


def save(settings: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    header = "# Written by ./deploy.sh. Safe to edit; re-run ./deploy.sh to apply.\n"
    SETTINGS_FILE.write_text(header + yaml.safe_dump(settings, sort_keys=False))
    SETTINGS_FILE.chmod(0o600)


def vpn_host(settings: dict[str, Any]) -> str:
    ns, zone = settings["vpn_ns"], settings["zone"]
    return f"{ns}.{zone}" if ns else zone


def hostnames(settings: dict[str, Any]) -> dict[str, str]:
    base = vpn_host(settings)
    labels = settings["labels"]
    return {
        "vpn": base,
        "auth": f"{labels['auth']}.{base}",
        "wg": f"{labels['wg']}.{base}",
        "adguard": f"{labels['adguard']}.{base}",
    }
