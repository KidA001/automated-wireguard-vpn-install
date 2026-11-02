"""Encrypted secrets (config/vault.yml) and the vault password in the OS keychain."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import string
from typing import Any

import bcrypt
import yaml
from ansible.parsing.vault import VaultLib, VaultSecret
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

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
    # Authelia
    "vault_authelia_session_secret": 64,
    "vault_authelia_storage_key": 64,
    "vault_authelia_jwt_secret": 64,
    "vault_authelia_oidc_hmac": 64,
    "vault_oidc_client_id": 32,
    "vault_oidc_client_secret": 64,
}

# Authelia's recommended argon2id parameters (its defaults).
ARGON2 = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
PBKDF2_ROUNDS = 310000


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


def totp_secret() -> str:
    """160-bit RFC 4226 secret, base32 without padding (what Authelia and authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _ab64(data: bytes) -> str:
    """passlib's "adapted base64", used by Authelia's $pbkdf2-sha512$ format."""
    return base64.b64encode(data).decode().rstrip("=").replace("+", ".")


def pbkdf2_sha512(secret: str, salt: bytes | None = None, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha512", secret.encode(), salt, rounds)
    return f"$pbkdf2-sha512${rounds}${_ab64(salt)}${_ab64(digest)}"


def pbkdf2_matches(secret: str, encoded: str) -> bool:
    try:
        _, scheme, rounds, salt, _digest = encoded.split("$")
        raw_salt = base64.b64decode(salt.replace(".", "+") + "=" * (-len(salt) % 4))
    except ValueError:
        return False
    return scheme == "pbkdf2-sha512" and secrets.compare_digest(
        pbkdf2_sha512(secret, raw_salt, int(rounds)), encoded
    )


def argon2_matches(password: str, encoded: str) -> bool:
    try:
        return ARGON2.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def rsa_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def ensure_generated(vault: dict[str, Any]) -> None:
    """Fill in machine-generated secrets that don't exist yet (never rotates existing ones).

    Hashes are recomputed only when the secret they hash changed, so re-runs don't rewrite
    server configs with a new salt."""
    for name, length in GENERATED.items():
        vault.setdefault(name, random_password(length))
    vault.setdefault("vault_totp_secret", totp_secret())
    vault.setdefault("vault_oidc_jwk", rsa_private_key_pem())
    if not argon2_matches(vault["vault_authelia_password"], vault.get("vault_authelia_password_hash", "")):
        vault["vault_authelia_password_hash"] = ARGON2.hash(vault["vault_authelia_password"])
    if not pbkdf2_matches(vault["vault_oidc_client_secret"], vault.get("vault_oidc_client_secret_hash", "")):
        vault["vault_oidc_client_secret_hash"] = pbkdf2_sha512(vault["vault_oidc_client_secret"])
    # Hashed once and stored, so re-runs don't rewrite AdGuard's config with a new salt.
    if not vault.get("vault_adguard_password_hash"):
        hashed = bcrypt.hashpw(vault["vault_adguard_password"].encode(), bcrypt.gensalt(10))
        vault["vault_adguard_password_hash"] = hashed.decode()
