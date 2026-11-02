"""Test helper: edit the VM test config (never your real one).

Usage (from the repo root):
  VPN_CONFIG_DIR=tests/vm/.config VPN_VAULT_PASSWORD=... uv run python tests/vm/set_config.py \
      settings.ssh_port=2222 vault.vault_sudo_password_pending=NewPass123456
"""

import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "tests/vm/.config" not in os.environ.get("VPN_CONFIG_DIR", ""):
    sys.exit("Refusing to run: VPN_CONFIG_DIR must point at tests/vm/.config")

from wizard import secrets_store  # noqa: E402
from wizard import settings as settings_mod  # noqa: E402

settings = settings_mod.load()
vault_pw = secrets_store.vault_password()
vault = secrets_store.load(vault_pw)

for arg in sys.argv[1:]:
    target, _, value = arg.partition("=")
    where, _, key = target.partition(".")
    parsed = yaml.safe_load(value) if value else None
    store = settings if where == "settings" else vault
    if parsed is None:
        store.pop(key, None)
    else:
        store[key] = parsed

settings_mod.save(settings)
secrets_store.save(vault_pw, vault)
print("updated:", ", ".join(sys.argv[1:]))
