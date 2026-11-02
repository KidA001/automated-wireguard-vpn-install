"""Detection helpers (timezone, DNS provider, user) and input validators."""

from __future__ import annotations

import getpass
import ipaddress
import os
import re
import secrets
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import dns.exception
import dns.resolver

# NS hostname substring -> provider name. First match wins.
PROVIDERS: list[tuple[str, str]] = [
    ("ns.cloudflare.com", "Cloudflare"),
    ("awsdns", "AWS Route 53"),
    ("porkbun", "Porkbun"),
    ("registrar-servers.com", "Namecheap"),
    ("domaincontrol.com", "GoDaddy"),
    ("googledomains.com", "Google Cloud DNS / Squarespace"),
    ("squarespacedns", "Squarespace"),
    ("digitalocean.com", "DigitalOcean"),
    ("linode.com", "Akamai/Linode"),
    ("hetzner", "Hetzner"),
    ("vultr.com", "Vultr"),
    ("gandi.net", "Gandi"),
    ("ovh.net", "OVH"),
    ("name.com", "Name.com"),
    ("dnsimple", "DNSimple"),
    ("nsone.net", "NS1"),
    ("azure-dns", "Azure DNS"),
    ("hover.com", "Hover"),
    ("dynadot", "Dynadot"),
]
PROVIDER_NAMES = sorted({name for _, name in PROVIDERS})
API_SUPPORTED = {"Cloudflare"}

LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)((?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED_USERS = {"root", "daemon", "bin", "sys", "nobody", "admin", "debian", "ubuntu", "docker"}


def local_username() -> str:
    name = getpass.getuser().lower()
    return name if USER_RE.match(name) and name not in RESERVED_USERS else "vpnadmin"


def timezone() -> tuple[str, bool]:
    """Return (IANA name, detected?)."""
    candidates: list[str] = []
    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = os.path.realpath(localtime)
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True, timeout=5
        )
        candidates.append(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    tzfile = Path("/etc/timezone")
    if tzfile.exists():
        candidates.append(tzfile.read_text().strip())
    for name in candidates:
        if valid_timezone(name):
            return name, True
    return "Etc/UTC", False


def valid_timezone(name: str) -> bool:
    if not name or name not in available_timezones():
        return False
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


def nameservers(zone: str) -> list[str]:
    """NS records for the zone, walking up to the parent if the name isn't a zone apex."""
    labels = zone.split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        try:
            answer = dns.resolver.resolve(name, "NS", lifetime=5)
            return sorted(str(r.target).rstrip(".").lower() for r in answer)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except dns.exception.DNSException:
            return []
    return []


def provider_for(ns_hosts: list[str]) -> str:
    for host in ns_hosts:
        for needle, name in PROVIDERS:
            if needle in host:
                return name
    return ""


def random_ula_cidr() -> str:
    """A random RFC 4193 ULA /48 prefix, used as a /112 for VPN clients."""
    groups = secrets.token_bytes(5)
    prefix = f"fd{groups[0]:02x}:{groups[1]:02x}{groups[2]:02x}:{groups[3]:02x}{groups[4]:02x}::/112"
    return canonical_cidr(prefix)


def canonical_cidr(cidr: str) -> str:
    """Shortest standard form (no leading zeros), e.g. fd63:e013:012b::/112 -> fd63:e013:12b::/112.

    AdGuard and other tools rewrite addresses this way; using it everywhere keeps deploys idempotent."""
    net = ipaddress.ip_network(cidr, strict=False)
    return f"{net.network_address.compressed}/{net.prefixlen}"


# --- validators (return True or an error message, as questionary expects) ---


def v_ipv4(value: str) -> bool | str:
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError:
        return "Enter an IPv4 address, e.g. 203.0.113.10"
    return True if not (ip.is_loopback or ip.is_unspecified or ip.is_multicast) else "Not a usable address."


def v_domain(value: str) -> bool | str:
    return True if DOMAIN_RE.match(value.lower()) else "Enter a domain like example.com"


def v_label(value: str, allow_empty: bool = False) -> bool | str:
    if value == "" and allow_empty:
        return True
    return True if LABEL_RE.match(value) else "Lowercase letters, digits and hyphens only."


def v_email(value: str) -> bool | str:
    return True if EMAIL_RE.match(value) else "Enter an email address."


def v_time(value: str) -> bool | str:
    return True if TIME_RE.match(value) else "Use 24-hour HH:MM, e.g. 02:00"


def v_port(value: str) -> bool | str:
    return True if value.isdigit() and 1 <= int(value) <= 65535 else "Enter a port between 1 and 65535."


def v_username(value: str) -> bool | str:
    if value in RESERVED_USERS:
        return f"'{value}' is reserved, pick another name."
    return True if USER_RE.match(value) else "Lowercase letters, digits, '_' or '-' (max 32)."


def v_username_or_root(value: str) -> bool | str:
    return True if USER_RE.match(value) else "Enter a Linux username, e.g. root or debian."


def v_duration(value: str) -> bool | str:
    return True if re.fullmatch(r"0|\d+[mhdw]", value) else "Use a duration like 30d, 12h, or 0 to disable."


def v_timezone(value: str) -> bool | str:
    return True if valid_timezone(value) else "Unknown timezone; use an IANA name like Europe/Berlin."
