"""The DNS records the user must create, and a gate that waits until they resolve."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import dns.exception
import dns.resolver

from wizard import ui
from wizard.settings import hostnames

PUBLIC_RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]


@dataclass(frozen=True)
class Record:
    type: str
    name: str  # fully qualified
    value: str

    def relative(self, zone: str) -> str:
        return "@" if self.name == zone else self.name.removesuffix(f".{zone}")


def required(settings: dict[str, Any], ipv4: str, ipv6: str | None) -> list[Record]:
    hosts = hostnames(settings)
    records = [Record("A", hosts["vpn"], ipv4)]
    if ipv6:
        records.append(Record("AAAA", hosts["vpn"], ipv6))
    for key in ("auth", "wg", "adguard", "dns"):
        records.append(Record("CNAME", hosts[key], hosts["vpn"]))
    return records


def show(settings: dict[str, Any], records: list[Record]) -> None:
    zone = settings["zone"]
    ui.table(
        f"DNS records for {zone} ({settings.get('dns_provider') or 'your DNS provider'})",
        ["Type", "Name", "Value", "Proxy"],
        [(r.type, r.relative(zone), r.value, "DNS only") for r in records],
    )
    ui.note(
        "If your provider can proxy traffic (e.g. Cloudflare's orange cloud), set every record to DNS only.\n"
        "Recommended manual extras: enable DNSSEC for the zone, and add CAA records allowing only "
        "letsencrypt.org and sectigo.com."
    )


def _resolves(record: Record, resolver_ip: str) -> bool:
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = [resolver_ip]
    resolver.lifetime = 5
    try:
        if record.type == "CNAME":
            answer = resolver.resolve(record.name, "CNAME")
            return any(str(r.target).rstrip(".") == record.value for r in answer)
        answer = resolver.resolve(record.name, record.type)
        return any(r.to_text() == record.value for r in answer)
    except dns.exception.DNSException:
        return False


def missing(records: list[Record]) -> list[Record]:
    return [r for r in records if not all(_resolves(r, ip) for ip in PUBLIC_RESOLVERS)]


def gate(records: list[Record], timeout_s: int = 1800) -> bool:
    """Poll public resolvers until every record resolves. Returns False if skipped/timed out."""
    pending = missing(records)
    if not pending:
        ui.success("All DNS records resolve on public resolvers.")
        return True
    if ui.NON_INTERACTIVE:
        ui.warn("Some DNS records don't resolve yet: " + ", ".join(f"{r.type} {r.name}" for r in pending))
        return False
    ui.info("Waiting for DNS to resolve (Ctrl-C to skip)…")
    deadline = time.monotonic() + timeout_s
    try:
        while pending and time.monotonic() < deadline:
            ui.note("  still waiting for: " + ", ".join(f"{r.type} {r.name}" for r in pending))
            time.sleep(15)
            pending = missing(pending)
    except KeyboardInterrupt:
        ui.warn("Skipped the DNS check. Certificates and the VPN endpoint won't work until DNS resolves.")
        return False
    if pending:
        ui.warn("Timed out waiting for DNS.")
        return False
    ui.success("All DNS records resolve on public resolvers.")
    return True
