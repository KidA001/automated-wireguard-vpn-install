"""./deploy.sh entry point: prompts → SSH bootstrap → DNS → playbook → summary."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import segno

from wizard import cloudflare, detect, dnsrecords, prompts, runner, secrets_store, sshboot, ui
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
    p.add_argument("--show-totp", action="store_true", help="show the authenticator QR code again and exit")
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


def apply_pending_sudo_password(settings: dict[str, Any], vault: dict[str, Any], vault_pw: str) -> None:
    """Switch the server to a newly chosen sudo password, keeping the old one until it's confirmed.

    Interruption-safe: if anything fails, the current password stays valid on the server
    and in the vault, and the pending one is retried on the next run.
    """
    pending = vault.get("vault_sudo_password_pending")
    if not pending:
        return
    ip, port, admin = settings["vps_ipv4"], settings["ssh_port_active"], settings["admin_user"]
    key = Path(settings["ssh_key_path"])
    current = vault["vault_sudo_password"]
    if pending != current and not sshboot.sudo_works(ip, port, admin, key, pending):
        if not sshboot.change_password(ip, port, admin, key, current, pending):
            raise SystemExit(
                "Couldn't change the sudo password on the server (the current one still works). "
                "Re-run ./deploy.sh to retry."
            )
        if not sshboot.sudo_works(ip, port, admin, key, pending):
            raise SystemExit(
                "The new sudo password didn't work after changing it. Re-run ./deploy.sh to retry."
            )
    vault["vault_sudo_password"] = pending
    del vault["vault_sudo_password_pending"]
    secrets_store.save(vault_pw, vault)
    ui.success(f"Changed the sudo password for {admin}")


def active_ssh_port(settings: dict[str, Any], previous: int) -> int:
    """The port SSH really answers on now (the configured one, or the old one if it didn't change)."""
    ip, admin, key = settings["vps_ipv4"], settings["admin_user"], Path(settings["ssh_key_path"])
    for candidate in dict.fromkeys([int(settings["ssh_port"]), previous]):
        if sshboot.can_login(ip, candidate, admin, key):
            return candidate
    raise SystemExit(
        f"Final SSH check failed on port {settings['ssh_port']} and {previous}! "
        "The server rolls back SSH changes within 3 minutes; then re-run ./deploy.sh."
    )


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


def totp_uri(settings: dict[str, Any], secret: str) -> str:
    issuer = quote(settings_mod.vpn_host(settings), safe="")
    user = quote(settings["admin_user"], safe="")
    return f"otpauth://totp/{issuer}:{user}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def show_totp(settings: dict[str, Any], vault: dict[str, Any]) -> None:
    ui.heading("Authenticator app")
    ui.info("Scan this QR code with your authenticator app (1Password, Google Authenticator, Aegis, …).")
    ui.info(f"It's your second sign-in step at https://{settings_mod.hostnames(settings)['auth']}.")
    segno.make(totp_uri(settings, vault["vault_totp_secret"]), error="m").terminal(compact=True)
    ui.note(f"Can't scan? Enter this key manually: {vault['vault_totp_secret']}")
    ui.note("Show it again any time with: ./deploy.sh --show-totp")


def summary(settings: dict[str, Any], alias: str, staging: bool) -> None:
    hosts = settings_mod.hostnames(settings)
    ui.heading("Done")
    ui.success(f"Server deployed: {hosts['vpn']} ({settings['vps_ipv4']})")
    ui.info(f"Sign in:        https://{hosts['auth']}   (user: {settings['admin_user']})")
    ui.info(f"WireGuard UI:   https://{hosts['wg']}")
    ui.info(f"AdGuard UI:     https://{hosts['adguard']}")
    ui.info(f"SSH:            ssh {alias}")
    if staging:
        ui.warn("--staging: certificates are from Let's Encrypt's test CA, so browsers will warn.")
        ui.warn("Run ./deploy.sh without --staging for real certificates.")
    ui.note(
        f"Status: ssh {alias} sudo vpn-status   ·   Passkey codes: ssh {alias} sudo vpn-status --auth-code"
    )


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
    if args.show_totp:
        if not vault.get("vault_totp_secret"):
            raise SystemExit("No authenticator secret yet; run ./deploy.sh first.")
        show_totp(settings, vault)
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
    if need_bootstrap and args.check:
        raise SystemExit(
            "The server isn't set up yet, so a dry run can't log in as the admin user. "
            "Nothing was changed. Run ./deploy.sh without --check to set it up."
        )
    if need_bootstrap:
        if vault.get("vault_sudo_password_pending"):
            # A freshly (re)built server: just use the new password from the start.
            vault["vault_sudo_password"] = vault.pop("vault_sudo_password_pending")
            secrets_store.save(vault_pw, vault)
        if login_plan is None:
            login_plan = prompts.ssh_access(settings, settings["vpn_ns"], settings["zone"])
        bootstrap(settings, vault, vault_pw, login_plan)
    elif not args.check:
        apply_pending_sudo_password(settings, vault, vault_pw)

    ip, key, admin = settings["vps_ipv4"], Path(settings["ssh_key_path"]), settings["admin_user"]
    port = settings["ssh_port_active"]
    ipv6 = server_ipv6(settings)
    records = dnsrecords.required(settings, ip, ipv6)
    ui.heading("DNS")
    managed = False
    if (
        settings.get("dns_provider") == "Cloudflare"
        and vault.get("vault_cloudflare_token")
        and not args.check
    ):
        try:
            cf = cloudflare.Client(vault["vault_cloudflare_token"])
            zone_id = cf.zone_id(settings["zone"])
            managed = cloudflare.apply(cf, zone_id, cloudflare.plan(cf, zone_id, records), settings["zone"])
        except cloudflare.CloudflareError as exc:
            ui.warn(f"Cloudflare: {exc}. Falling back to the record table below.")
    if not managed:
        dnsrecords.show(settings, records)
    if not args.skip_dns_check:
        dnsrecords.gate(records)

    extra: dict[str, Any] = {
        "admin_pubkey": sshboot.public_key(key),
        "deployer_ip": sshboot.client_ip(ip, port, admin, key),
        "tls_mode": "staging" if args.staging else "acme",
        "server_ipv6": ipv6 or "",
    }
    # Developer/test overrides (e.g. tests/vm), never set by the prompts.
    extra.update(json.loads(os.environ.get("VPN_EXTRA_VARS", "{}")))

    if not args.check:
        # The ssh role reconnects on the new port with strict host key checking.
        sshboot.pin_extra_port(ip, port, int(settings["ssh_port"]))
    runner.write_inventory(settings, admin, port)
    runner.playbook("site.yml", vault_pw, extra_vars=extra, tags=args.tags, check=args.check)

    if args.check:
        ui.success("Dry run finished; nothing was changed.")
        return 0
    active = active_ssh_port(settings, port)
    if active != settings["ssh_port_active"]:
        settings["ssh_port_active"] = active
        settings_mod.save(settings)
    if active != int(settings["ssh_port"]):
        ui.warn(f"SSH is still on port {active}; run a full ./deploy.sh (without --tags) to move it.")
    ui.success(f"Final SSH login check passed (port {active})")

    alias = settings["vpn_ns"] or "vpn"
    block = sshboot.ssh_config_block(alias, ip, active, admin, key)
    if not os.environ.get("VPN_SKIP_SSH_CONFIG") and ui.confirm(
        f"Add/update a 'Host {alias}' entry in ~/.ssh/config?", default=True
    ):
        sshboot.ensure_ssh_config(alias, block)
    if not settings.get("totp_shown"):
        show_totp(settings, vault)
        if ui.confirm("Added it to your authenticator app?", default=True):
            settings["totp_shown"] = True
            settings_mod.save(settings)
    summary(settings, alias, args.staging)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Command failed: {exc}") from exc
