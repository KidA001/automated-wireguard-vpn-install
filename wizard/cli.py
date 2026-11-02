"""./deploy.sh entry point: prompts → SSH bootstrap → DNS → playbook → summary."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from wizard import detect, dnsrecords, prompts, runner, secrets_store, sshboot, ui
from wizard import settings as settings_mod


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="deploy.sh", description="Deploy or update your WireGuard VPN server.")
    p.add_argument("--tags", help="only run these roles (comma separated)")
    p.add_argument("--check", action="store_true", help="dry run (show what would change)")
    p.add_argument("--non-interactive", action="store_true", help="use saved answers, no prompts")
    p.add_argument("--staging", action="store_true", help="use Let's Encrypt staging certificates")
    p.add_argument("--reconfigure", action="store_true", help="walk through every question again")
    p.add_argument("--skip-dns-check", action="store_true", help="don't wait for DNS records to resolve")
    p.add_argument(
        "--show-secret", metavar="NAME", help="print one vault secret (e.g. root_password) and exit"
    )
    return p.parse_args(argv)


def admin_login_works(settings: dict[str, Any]) -> bool:
    key = settings.get("ssh_key_path")
    if not (settings.get("bootstrapped") and key and settings.get("vps_ipv4")):
        return False
    ip, port, admin = settings["vps_ipv4"], settings["ssh_port_active"], settings["admin_user"]
    if sshboot.can_login(ip, port, admin, Path(key)):
        return True
    # A changed host key (rebuild, or the provider regenerated keys) also breaks login.
    # Re-pin (with confirmation) and retry before falling back to a full bootstrap.
    if sshboot.host_key_changed(ip, port):
        sshboot.pin_host_key(ip, port)
        return sshboot.can_login(ip, port, admin, Path(key))
    return False


def bootstrap(settings: dict[str, Any], vault: dict[str, Any], vault_pw: str, plan: dict[str, Any]) -> None:
    ui.heading("Bootstrapping the server")
    ip, key = settings["vps_ipv4"], Path(settings["ssh_key_path"])
    port = settings["ssh_port_active"] = int(os.environ.get("VPN_BOOTSTRAP_PORT", "22"))
    user = plan["user"]
    sshboot.pin_host_key(ip, port)
    pubkey = sshboot.public_key(key)
    if plan.get("password"):
        sshboot.install_key_with_password(ip, port, user, plan["password"], pubkey)
        ui.success(f"Installed your public key for {user}")
    if not sshboot.can_login(ip, port, user, key):
        raise SystemExit(f"Key login as {user}@{ip} failed. Check the key/user and try again.")
    ui.success(f"Key login as {user} works")

    become_pw = None
    if user != "root" and sshboot.run(ip, port, user, key, "sudo -n true").returncode != 0:
        become_pw = os.environ.get("VPN_BOOTSTRAP_SUDO_PASSWORD") or ui.secret(f"sudo password for {user}")
    runner.write_inventory(settings, user, port)
    runner.playbook(
        "bootstrap.yml",
        vault_pw,
        extra_vars={"admin_pubkey": pubkey, "bootstrap_user": user},
        become_password=become_pw,
    )

    admin = settings["admin_user"]
    if not sshboot.can_login(ip, port, admin, key):
        raise SystemExit(f"Couldn't log in as {admin} after bootstrap; nothing was locked down.")
    if not sshboot.sudo_works(ip, port, admin, key, vault["vault_sudo_password"]):
        raise SystemExit(f"sudo for {admin} failed after bootstrap; nothing was locked down.")
    ui.success(f"Verified key login and sudo for {admin}")
    settings["bootstrapped"] = True
    settings_mod.save(settings)


def server_ipv6(settings: dict[str, Any]) -> str | None:
    """The VPS's public IPv6 address, if it has one."""
    result = sshboot.run(
        settings["vps_ipv4"],
        settings["ssh_port_active"],
        settings["admin_user"],
        Path(settings["ssh_key_path"]),
        "ip -6 -o route get 2001:4860:4860::8888 2>/dev/null",
    )
    parts = result.stdout.split()
    if "src" in parts:
        addr = parts[parts.index("src") + 1]
        if ipaddress.IPv6Address(addr).is_global:
            return addr
    return None


def summary(settings: dict[str, Any], alias: str) -> None:
    hosts = settings_mod.hostnames(settings)
    ui.heading("Done")
    ui.success(f"Server deployed: {hosts['vpn']} ({settings['vps_ipv4']})")
    ui.info(f"SSH:            ssh {alias}")
    ui.info(f"WireGuard UI:   ssh -N -L 51821:127.0.0.1:51821 {alias}  →  http://localhost:51821")
    ui.info(f"AdGuard UI:     ssh -N -L 3000:127.0.0.1:3000 {alias}  →  http://localhost:3000")
    ui.note("Web UIs are only reachable through an SSH tunnel until the login front door (phase b) is added.")
    ui.note("Passwords: ./deploy.sh --show-secret wg_easy_password | adguard_password")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ui.NON_INTERACTIVE = args.non_interactive

    vault_pw = secrets_store.vault_password()
    vault = secrets_store.load(vault_pw)
    settings = settings_mod.load()

    if args.show_secret:
        name = args.show_secret if args.show_secret.startswith("vault_") else f"vault_{args.show_secret}"
        if name not in vault:
            raise SystemExit(
                f"No secret '{args.show_secret}'. Known: " + ", ".join(k[6:] for k in sorted(vault))
            )
        print(vault[name])
        return 0

    ui.heading("WireGuard VPN server")
    first_run = not settings_mod.SETTINGS_FILE.exists()
    need_bootstrap = not admin_login_works(settings)
    login_plan = None
    if first_run or args.reconfigure or not args.non_interactive:
        login_plan = prompts.essentials(settings, vault, need_bootstrap)
        prompts.advanced(settings)
    elif need_bootstrap:
        login_plan = prompts.ssh_access(settings, settings["vpn_ns"], settings["zone"])

    for required in ("vps_ipv4", "admin_user", "zone", "ssh_key_path", "timezone"):
        if not settings.get(required):
            raise SystemExit(f"Missing setting '{required}'. Run ./deploy.sh interactively.")
    if not settings["wg_ipv6_cidr"]:
        settings["wg_ipv6_cidr"] = detect.random_ula_cidr()
    if not vault.get("vault_authelia_password"):
        vault["vault_authelia_password"] = secrets_store.random_password(24)
        ui.warn("Generated a web login password: ./deploy.sh --show-secret authelia_password")
    secrets_store.ensure_generated(vault)
    settings_mod.save(settings)
    secrets_store.save(vault_pw, vault)

    runner.install_collections()

    need_bootstrap = not admin_login_works(settings)
    if need_bootstrap:
        if login_plan is None:
            login_plan = prompts.ssh_access(settings, settings["vpn_ns"], settings["zone"])
        bootstrap(settings, vault, vault_pw, login_plan)

    ip, key, admin = settings["vps_ipv4"], Path(settings["ssh_key_path"]), settings["admin_user"]
    port = settings["ssh_port_active"]
    ipv6 = server_ipv6(settings)
    records = dnsrecords.required(settings, ip, ipv6)
    ui.heading("DNS")
    dnsrecords.show(settings, records)
    if not args.skip_dns_check:
        dnsrecords.gate(records)

    extra: dict[str, Any] = {
        "admin_pubkey": sshboot.public_key(key),
        "deployer_ip": sshboot.client_ip(ip, port, admin, key),
        "acme_staging": args.staging,
        "server_ipv6": ipv6 or "",
    }
    # Developer/test overrides (e.g. tests/vm), never set by the prompts.
    extra.update(json.loads(os.environ.get("VPN_EXTRA_VARS", "{}")))

    runner.write_inventory(settings, admin, port)
    runner.playbook("site.yml", vault_pw, extra_vars=extra, tags=args.tags, check=args.check)

    if not args.check and settings["ssh_port"] != port:
        settings["ssh_port_active"] = settings["ssh_port"]
        settings_mod.save(settings)
    if not sshboot.can_login(ip, settings["ssh_port_active"], admin, key):
        raise SystemExit("Final SSH check failed! The server rolls back SSH changes within 2 minutes.")
    ui.success("Final SSH login check passed")

    alias = settings["vpn_ns"] or "vpn"
    block = sshboot.ssh_config_block(alias, ip, settings["ssh_port_active"], admin, key)
    if not os.environ.get("VPN_SKIP_SSH_CONFIG") and ui.confirm(
        f"Add/update a 'Host {alias}' entry in ~/.ssh/config?", default=True
    ):
        sshboot.ensure_ssh_config(alias, block)
    summary(settings, alias)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed: {exc}") from exc
