"""Encrypted secrets (config/vault.yml) and the vault password in the OS keychain."""

from __future__ import annotations

import os
import secrets
import string
from typing import Any

import bcrypt
import yaml
from ansible.parsing.vault import VaultLib, VaultSecret

from wizard import ui
from wizard.settings import CONFIG_DIR, VAULT_FILE

KEYCHAIN_SERVICE = "wireguard-vpn-server"
KEYCHAIN_ACCOUNT = f"vault:{CONFIG_DIR}"
ENV_VAR = "VPN_VAULT_PASSWORD"

# Secrets generated automatically when missing: name -> length.
GENERATED = {
    "vault_sudo_password": 24,
    "vault_root_password": 32,
    "vault_wg_easy_password": 32,
    "vault_adguard_password": 32,
}


def random_password(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _keyring():
    try:
        import keyring
        from keyring.errors import KeyringError

        keyring.get_keyring()
        return keyring, KeyringError
    except Exception:  # noqa: BLE001 - any backend failure means "no keychain"
        return None, Exception


def vault_password() -> str:
    """Return the vault password, creating and storing one on first use."""
    if os.environ.get(ENV_VAR):
        return os.environ[ENV_VAR]
    keyring, keyring_error = _keyring()
    if keyring is not None:
        try:
            stored = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
            if stored:
                return stored
            if VAULT_FILE.exists():
                ui.warn("A vault exists but its password isn't in your keychain.")
                pw = ui.secret("Vault password", allow_empty=False)
            else:
                pw = random_password(40)
            keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, pw)
            return pw
        except keyring_error as exc:
            ui.warn(f"Keychain unavailable ({exc}); falling back to a typed vault password.")
    if VAULT_FILE.exists():
        return ui.secret("Vault password", allow_empty=False)
    ui.info("No OS keychain found. Choose a vault password (you'll be asked for it on every run).")
    return ui.secret("New vault password", confirm=True, allow_empty=False, min_length=12)


def _vault_lib(password: str) -> VaultLib:
    return VaultLib([("default", VaultSecret(password.encode()))])


def load(password: str) -> dict[str, Any]:
    if not VAULT_FILE.exists():
        return {}
    plaintext = _vault_lib(password).decrypt(VAULT_FILE.read_bytes())
    return yaml.safe_load(plaintext) or {}


def save(password: str, data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    plaintext = yaml.safe_dump(data, sort_keys=True).encode()
    VAULT_FILE.write_bytes(_vault_lib(password).encrypt(plaintext))
    VAULT_FILE.chmod(0o600)


def ensure_generated(vault: dict[str, Any]) -> None:
    """Fill in machine-generated secrets that don't exist yet (never rotates existing ones)."""
    for name, length in GENERATED.items():
        vault.setdefault(name, random_password(length))
    # Hashed once and stored, so re-runs don't rewrite AdGuard's config with a new salt.
    if not vault.get("vault_adguard_password_hash"):
        hashed = bcrypt.hashpw(vault["vault_adguard_password"].encode(), bcrypt.gensalt(10))
        vault["vault_adguard_password_hash"] = hashed.decode()
