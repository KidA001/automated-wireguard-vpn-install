"""Server state kept off the server: identity in the vault, and full encrypted backups.

Identity (default on): a SQL dump of wg-easy's database (WireGuard server key, devices and
their keys) is saved in the local vault after every deploy. A rebuilt server is seeded with it,
so devices reconnect unchanged. The TOTP secret and the IPv6 prefix are already kept locally.

Full backups (opt-in): wg-easy's and Authelia's databases (adds passkeys and Authelia state),
encrypted on the server to an age public key. Only this machine's vault holds the private key.
"""

from __future__ import annotations

import base64
import gzip
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

import pyrage

from wizard import secrets_store, sshboot, ui
from wizard.settings import CONFIG_DIR, REPO, vpn_host

WG_EASY_DB = "/opt/vpn/wg-easy/data/wg-easy.db"
AUTHELIA_DB = "/opt/vpn/authelia/config/db.sqlite3"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
BACKUP_DIR = REPO / "backups"
KEEP_BACKUPS = 10


def _ssh(settings: dict[str, Any]) -> tuple[str, int, str, Path]:
    return (
        settings["vps_ipv4"],
        settings["ssh_port_active"],
        settings["admin_user"],
        Path(settings["ssh_key_path"]),
    )


def server_is_new(settings: dict[str, Any]) -> bool:
    ip, port, user, key = _ssh(settings)
    return sshboot.run(ip, port, user, key, "test -e /var/lib/vpn/installed").returncode != 0


def device_count(dump: str) -> int:
    """Number of WireGuard devices in a wg-easy SQL dump."""
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(dump)
        return db.execute("select count(*) from clients_table").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        db.close()


def encode_dump(dump: str) -> str:
    return base64.b64encode(gzip.compress(dump.encode(), mtime=0)).decode()


def decode_dump(blob: str) -> str:
    return gzip.decompress(base64.b64decode(blob)).decode()


def export_identity(settings: dict[str, Any], vault: dict[str, Any], vault_pw: str) -> None:
    """Save the wg-easy database (server key + devices) into the vault."""
    ip, port, user, key = _ssh(settings)
    raw = sshboot.sudo_output(
        ip, port, user, key, vault["vault_sudo_password"], f"sqlite3 -readonly {WG_EASY_DB} .dump"
    )
    dump = raw.decode()
    if "CREATE TABLE" not in dump:
        ui.warn("Couldn't read wg-easy's database; the identity in your vault was not updated.")
        return
    # wg-easy links your account to Authelia's opaque user ID (the OIDC "sub"); a rebuilt Authelia
    # must reuse it or single sign-on fails with "linked with a different account".
    # admin_user is validated as [a-z_][a-z0-9_-]* by the prompts, so it's safe to inline.
    sql = (
        "select identifier from user_opaque_identifier where service='openid' and sector_id='' and username="
    )
    query = f"sqlite3 -readonly {AUTHELIA_DB} \"{sql}'{settings['admin_user']}'\""
    subject = sshboot.sudo_output(ip, port, user, key, vault["vault_sudo_password"], query).decode().strip()
    if UUID_RE.match(subject) and vault.get("vault_authelia_oidc_subject") != subject:
        vault["vault_authelia_oidc_subject"] = subject
        secrets_store.save(vault_pw, vault)
    old = vault.get("vault_wg_easy_dump")
    new = encode_dump(dump)
    if old == new:
        return
    before = device_count(decode_dump(old)) if old else 0
    vault["vault_wg_easy_dump"] = new
    secrets_store.save(vault_pw, vault)
    after = device_count(dump)
    change = f" ({after - before:+d} since the last save)" if old and after != before else ""
    ui.success(f"Saved the server identity to your vault: WireGuard key and {after} device(s){change}")


def identity_restore_file(vault: dict[str, Any]) -> Path | None:
    """Write the saved wg-easy dump to a private temp file for Ansible to upload."""
    blob = vault.get("vault_wg_easy_dump")
    if not blob:
        return None
    path = Path(tempfile.mkstemp(prefix="wg-easy-", suffix=".sql", dir=CONFIG_DIR)[1])
    path.write_text(decode_dump(blob))
    path.chmod(0o600)
    return path


# --- full backups ------------------------------------------------------------------------


def ensure_backup_keys(vault: dict[str, Any]) -> None:
    if not vault.get("vault_backup_identity"):
        identity = pyrage.x25519.Identity.generate()
        vault["vault_backup_identity"] = str(identity)
        vault["vault_backup_recipient"] = str(identity.to_public())


def backups(settings: dict[str, Any]) -> list[Path]:
    prefix = re.sub(r"[^a-z0-9.-]", "_", vpn_host(settings))
    return sorted(BACKUP_DIR.glob(f"{prefix}-*.tar.gz.age"))


def take_backup(settings: dict[str, Any], vault: dict[str, Any]) -> Path:
    ip, port, user, key = _ssh(settings)
    data = sshboot.sudo_output(ip, port, user, key, vault["vault_sudo_password"], "vpn-backup-export")
    if not data.startswith(b"age-encryption.org/"):
        raise SystemExit("The server returned something that isn't an age-encrypted backup.")
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    prefix = re.sub(r"[^a-z0-9.-]", "_", vpn_host(settings))
    path = BACKUP_DIR / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz.age"
    path.write_bytes(data)
    path.chmod(0o600)
    for old in backups(settings)[:-KEEP_BACKUPS]:
        old.unlink()
    ui.success(f"Encrypted backup saved: {path.relative_to(REPO)}")
    return path


def backup_restore_file(path: Path, vault: dict[str, Any]) -> Path:
    identity = pyrage.x25519.Identity.from_str(vault["vault_backup_identity"])
    plain = pyrage.decrypt(path.read_bytes(), [identity])
    out = Path(tempfile.mkstemp(prefix="backup-", suffix=".tar.gz", dir=CONFIG_DIR)[1])
    out.write_bytes(plain)
    out.chmod(0o600)
    return out
