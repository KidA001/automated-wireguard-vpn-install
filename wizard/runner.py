"""Generates the inventory and runs ansible-playbook from the project venv."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from wizard import ui
from wizard.secrets_store import ENV_VAR
from wizard.settings import ANSIBLE_DIR, COLLECTIONS_DIR, INVENTORY_FILE, SETTINGS_FILE, VAULT_FILE

BIN = Path(sys.executable).parent


def _env(vault_password: str) -> dict[str, str]:
    return {
        **os.environ,
        "ANSIBLE_CONFIG": str(ANSIBLE_DIR / "ansible.cfg"),
        "ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS_DIR),
        ENV_VAR: vault_password,
    }


def install_collections() -> None:
    marker = COLLECTIONS_DIR / ".requirements.yml"
    requirements = ANSIBLE_DIR / "requirements.yml"
    if marker.exists() and marker.read_text() == requirements.read_text():
        return
    ui.info("Installing Ansible collections…")
    subprocess.run(
        [str(BIN / "ansible-galaxy"), "collection", "install", "--force", "-r", str(requirements),
         "-p", str(COLLECTIONS_DIR)],
        check=True,
        stdout=subprocess.DEVNULL,
        env={**os.environ, "ANSIBLE_COLLECTIONS_PATH": str(COLLECTIONS_DIR)},
    )  # fmt: skip
    marker.write_text(requirements.read_text())


def write_inventory(settings: dict[str, Any], user: str, port: int) -> None:
    host: dict[str, Any] = {
        "ansible_host": settings["vps_ipv4"],
        "ansible_port": port,
        "ansible_user": user,
        "ansible_ssh_private_key_file": settings["ssh_key_path"],
    }
    if user != "root":
        host["ansible_become_password"] = "{{ vault_sudo_password }}"  # noqa: S105 - Jinja reference
    inventory = {"all": {"hosts": {"vps": host}}}
    INVENTORY_FILE.write_text(yaml.safe_dump(inventory, sort_keys=False))
    INVENTORY_FILE.chmod(0o600)


def playbook(
    name: str,
    vault_password: str,
    extra_vars: dict[str, Any] | None = None,
    tags: str | None = None,
    check: bool = False,
    become_password: str | None = None,
) -> None:
    args = [
        str(BIN / "ansible-playbook"),
        name,
        "-i", str(INVENTORY_FILE),
        "-e", f"@{SETTINGS_FILE}",
        "-e", f"@{VAULT_FILE}",
        "--vault-password-file", str(ANSIBLE_DIR / "vault-pass.sh"),
    ]  # fmt: skip
    if extra_vars:
        args += ["-e", json.dumps(extra_vars)]
    if tags:
        args += ["--tags", tags]
    if check:
        args += ["--check", "--diff"]
    env = _env(vault_password)
    if become_password is not None:
        # Only used during bootstrap with a non-root login user that needs a sudo password.
        args += ["-e", json.dumps({"ansible_become_password": become_password})]
    ui.heading(f"ansible-playbook {name}")
    result = subprocess.run(args, cwd=ANSIBLE_DIR, env=env)
    if result.returncode != 0:
        raise SystemExit(
            f"Playbook {name} failed (exit {result.returncode}). Fix the error above and re-run."
        )
