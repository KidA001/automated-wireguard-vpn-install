"""SSH helpers: key selection/generation, host-key pinning, and login verification."""

from __future__ import annotations

import os
import platform
import subprocess
import tempfile
from pathlib import Path

from wizard import ui

SSH_DIR = Path.home() / ".ssh"
KNOWN_HOSTS = SSH_DIR / "known_hosts"


def host_token(ip: str, port: int) -> str:
    return ip if port == 22 else f"[{ip}]:{port}"


def local_keys() -> list[Path]:
    """Private keys in ~/.ssh that have a matching .pub file."""
    if not SSH_DIR.is_dir():
        return []
    return sorted(p.with_suffix("") for p in SSH_DIR.glob("*.pub") if p.with_suffix("").is_file())


def public_key(private_key: Path) -> str:
    pub = Path(f"{private_key}.pub")
    if pub.exists():
        return pub.read_text().strip()
    out = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(private_key)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def generate_key(path: Path, comment: str, passphrase: str) -> None:
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-a", "100", "-f", str(path), "-C", comment, "-N", passphrase],
        check=True,
    )
    if passphrase and platform.system() == "Darwin":
        # Store the passphrase in the macOS keychain so Ansible can use the key unattended.
        subprocess.run(["ssh-add", "--apple-use-keychain", str(path)], check=False)


def _fingerprint(key_line: str) -> str:
    out = subprocess.run(
        ["ssh-keygen", "-lf", "-"], input=key_line, capture_output=True, text=True, check=True
    )
    return out.stdout.split()[1]


def scan_host_key(ip: str, port: int) -> str:
    out = subprocess.run(
        ["ssh-keyscan", "-T", "10", "-p", str(port), "-t", "ed25519", ip], capture_output=True, text=True
    )
    lines = [line for line in out.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise SystemExit(
            f"Couldn't reach SSH on {ip}:{port}. Is the VPS running and the IP/port correct?\n"
            "If it was working a moment ago, your IP may be temporarily banned by fail2ban (1 hour);\n"
            "use your provider's web console: sudo fail2ban-client unban --all"
        )
    return lines[0]


def pinned_keys(ip: str, port: int) -> list[str]:
    if not KNOWN_HOSTS.exists():
        return []
    out = subprocess.run(
        ["ssh-keygen", "-F", host_token(ip, port), "-f", str(KNOWN_HOSTS)], capture_output=True, text=True
    )
    return [line for line in out.stdout.splitlines() if line and not line.startswith("#")]


def host_key_changed(ip: str, port: int) -> bool:
    """True if a different key is pinned for this host than the one it presents now."""
    existing = pinned_keys(ip, port)
    if not existing:
        return False
    try:
        scanned_key = " ".join(scan_host_key(ip, port).split()[1:3])
    except SystemExit:
        return False
    return not any(scanned_key in line for line in existing)


def pin_host_key(ip: str, port: int) -> None:
    """Trust-on-first-use with an explicit fingerprint confirmation."""
    scanned = scan_host_key(ip, port)
    scanned_key = " ".join(scanned.split()[1:3])
    existing = pinned_keys(ip, port)
    if any(scanned_key in line for line in existing):
        return
    fingerprint = _fingerprint(scanned)
    if existing:
        ui.warn(
            f"The host key for {host_token(ip, port)} has CHANGED. That's expected if you rebuilt the VPS; "
            "otherwise someone may be intercepting the connection."
        )
        accept = bool(os.environ.get("VPN_ACCEPT_NEW_HOSTKEY"))  # tests only
        if not ui.confirm("Replace the old host key?", default=accept):
            raise SystemExit("Aborted: host key mismatch.")
        subprocess.run(
            ["ssh-keygen", "-R", host_token(ip, port), "-f", str(KNOWN_HOSTS)], capture_output=True
        )
    ui.info(f"Server host key fingerprint: [bold]{fingerprint}[/bold] (ED25519)")
    ui.note("You can compare it with the fingerprint shown in your provider's web console.")
    if not ui.confirm("Does this match / do you trust this server?", default=True):
        raise SystemExit("Aborted: host key not trusted.")
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    with KNOWN_HOSTS.open("a") as fh:
        fh.write(f"{host_token(ip, port)} {scanned_key}\n")
    ui.success("Host key pinned in ~/.ssh/known_hosts")


def base_ssh_args(ip: str, port: int, user: str, key: Path | None) -> list[str]:
    args = [
        "ssh",
        "-p", str(port),
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "ConnectTimeout=15",
    ]  # fmt: skip
    if key:
        args += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    return [*args, f"{user}@{ip}"]


def run(ip: str, port: int, user: str, key: Path, command: str, stdin: str | None = None):
    return subprocess.run(
        [*base_ssh_args(ip, port, user, key)[:-1], "-o", "BatchMode=yes", f"{user}@{ip}", command],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def can_login(ip: str, port: int, user: str, key: Path) -> bool:
    try:
        return run(ip, port, user, key, "true").returncode == 0
    except subprocess.TimeoutExpired:
        return False


def sudo_works(ip: str, port: int, user: str, key: Path, password: str) -> bool:
    result = run(ip, port, user, key, "sudo -k; sudo -S -p '' -v && echo SUDO_OK", stdin=password + "\n")
    return "SUDO_OK" in result.stdout


def client_ip(ip: str, port: int, user: str, key: Path) -> str:
    """Our public IP as the server sees it (no third-party lookup)."""
    result = run(ip, port, user, key, "echo ${SSH_CLIENT%% *}")
    return result.stdout.strip()


def install_key_with_password(ip: str, port: int, user: str, password: str, pubkey: str) -> None:
    """Append pubkey to the user's authorized_keys, authenticating with a password."""
    script = (
        "umask 077; mkdir -p ~/.ssh; k=$(cat); "
        'grep -qxF "$k" ~/.ssh/authorized_keys 2>/dev/null || printf "%s\\n" "$k" >> ~/.ssh/authorized_keys'
    )
    with tempfile.TemporaryDirectory() as tmp:
        askpass = Path(tmp) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf "%s\\n" "$VPN_SSH_PASSWORD"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            "SSH_ASKPASS": str(askpass),
            "SSH_ASKPASS_REQUIRE": "force",
            "VPN_SSH_PASSWORD": password,
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
        args = base_ssh_args(ip, port, user, None)
        args[1:1] = [
            "-o", "PreferredAuthentications=password,keyboard-interactive",
            "-o", "PubkeyAuthentication=no",
            "-o", "NumberOfPasswordPrompts=1",
        ]  # fmt: skip
        result = subprocess.run(
            [*args, script], input=pubkey + "\n", env=env, capture_output=True, text=True, timeout=60
        )
    if result.returncode != 0:
        raise SystemExit(f"Password login as {user} failed: {result.stderr.strip() or 'wrong password?'}")


def ssh_config_block(alias: str, ip: str, port: int, user: str, key: Path) -> str:
    return (
        f"Host {alias}\n"
        f"    HostName {ip}\n"
        f"    User {user}\n"
        f"    Port {port}\n"
        f"    IdentityFile {key}\n"
        f"    IdentitiesOnly yes\n"
    )


def _remove_unmanaged_host(text: str, alias: str, marker_start: str, marker_end: str) -> tuple[str, bool]:
    """Drop a hand-written 'Host <alias>' stanza (outside our markers). Returns (text, removed?)."""
    out: list[str] = []
    skipping = in_managed = removed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == marker_start:
            in_managed = True
        if not in_managed:
            words = stripped.split()
            if words and words[0].lower() in ("host", "match"):
                skipping = words[0].lower() == "host" and words[1:] == [alias]
                removed = removed or skipping
        if stripped == marker_end:
            in_managed = False
        if not skipping:
            out.append(line)
    return "".join(out), removed


def ensure_ssh_config(alias: str, block: str) -> None:
    config = SSH_DIR / "config"
    current = config.read_text() if config.exists() else ""
    marker_start, marker_end = f"# >>> wireguard-vpn-server {alias}", f"# <<< wireguard-vpn-server {alias}"
    without_old, had_old = _remove_unmanaged_host(current, alias, marker_start, marker_end)
    if had_old:
        ui.warn(f"~/.ssh/config already has your own 'Host {alias}' entry; SSH would use it instead of ours.")
        if not ui.confirm("Replace it? (a backup is saved to ~/.ssh/config.bak)", default=True):
            ui.note(f"Left ~/.ssh/config unchanged. Use: ssh -i <key> <user>@<ip> instead of 'ssh {alias}'.")
            return
        (SSH_DIR / "config.bak").write_text(current)
        (SSH_DIR / "config.bak").chmod(0o600)
        current = without_old
    managed = f"{marker_start}\n{block}{marker_end}\n"
    if marker_start in current:
        before, rest = current.split(marker_start, 1)
        after = rest.split(marker_end, 1)[1].lstrip("\n") if marker_end in rest else ""
        new = before + managed + after
    else:
        new = (current.rstrip("\n") + "\n\n" if current else "") + managed
    if new != current:
        config.write_text(new)
        config.chmod(0o600)
