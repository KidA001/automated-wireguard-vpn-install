"""Optional Cloudflare DNS automation: show the changes, apply only after confirmation.

The token needs Zone → DNS → Edit on one zone. It never leaves this machine (encrypted vault).
Records are always DNS-only (proxied: false). Nothing is ever deleted, except a conflicting
record at one of our names, and only after a separate explicit confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from wizard import ui
from wizard.dnsrecords import Record

API = "https://api.cloudflare.com/client/v4"
COMMENT = "managed-by: wireguard-vpn-server"


class CloudflareError(Exception):
    pass


@dataclass
class Change:
    action: str  # create | update | replace | unchanged | conflict
    record: Record
    current: list[dict[str, Any]]

    @property
    def needs_write(self) -> bool:
        return self.action in ("create", "update", "replace")


class Client:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self.http = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
            transport=transport,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self.http.request(method, path, **kwargs)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudflareError(f"Cloudflare API request failed: {exc}") from exc
        if not data.get("success"):
            errors = (
                "; ".join(e.get("message", "?") for e in data.get("errors", [])) or f"HTTP {resp.status_code}"
            )
            raise CloudflareError(errors)
        return data["result"]

    def zone_id(self, zone: str) -> str:
        zones = self._call("GET", "/zones", params={"name": zone})
        if not zones:
            raise CloudflareError(
                f"The token can't see the zone {zone}. Give it Zone → DNS → Edit for that zone "
                "(Zone Resources: Include → Specific zone)."
            )
        return zones[0]["id"]

    def records(self, zone_id: str, name: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/zones/{zone_id}/dns_records", params={"name": name, "per_page": 100})

    def create(self, zone_id: str, rec: Record) -> None:
        body = {"type": rec.type, "name": rec.name, "content": rec.value, "proxied": False, "ttl": 1}
        self._call("POST", f"/zones/{zone_id}/dns_records", json={**body, "comment": COMMENT})

    def update(self, zone_id: str, record_id: str, rec: Record) -> None:
        body = {"type": rec.type, "name": rec.name, "content": rec.value, "proxied": False, "ttl": 1}
        self._call("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json={**body, "comment": COMMENT})

    def delete(self, zone_id: str, record_id: str) -> None:
        self._call("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


def _matches(existing: dict[str, Any], rec: Record) -> bool:
    content = existing.get("content", "").rstrip(".")
    return existing.get("type") == rec.type and content == rec.value and not existing.get("proxied")


def plan(client: Client, zone_id: str, wanted: list[Record]) -> list[Change]:
    changes = []
    for rec in wanted:
        current = [r for r in client.records(zone_id, rec.name) if r.get("type") in ("A", "AAAA", "CNAME")]
        same_type = [r for r in current if r["type"] == rec.type]
        # A CNAME can't coexist with anything; A/AAAA can't coexist with a CNAME.
        conflicting = [r for r in current if r["type"] != rec.type and ("CNAME" in (rec.type, r["type"]))]
        if conflicting:
            changes.append(Change("replace", rec, conflicting))
        elif not same_type:
            changes.append(Change("create", rec, []))
        elif len(same_type) > 1:
            changes.append(Change("conflict", rec, same_type))
        elif _matches(same_type[0], rec):
            changes.append(Change("unchanged", rec, same_type))
        else:
            changes.append(Change("update", rec, same_type))
    return changes


def _describe(records: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{r['type']} {r.get('content', '')}{' (proxied)' if r.get('proxied') else ''}" for r in records
    )


def show(changes: list[Change], zone: str) -> None:
    labels = {
        "create": "[green]CREATE[/green]",
        "update": "[yellow]UPDATE[/yellow]",
        "replace": "[red]REPLACE[/red]",
        "conflict": "[red]CONFLICT[/red]",
        "unchanged": "[dim]unchanged[/dim]",
    }
    ui.table(
        f"Cloudflare DNS changes for {zone}",
        ["Action", "Type", "Name", "Now", "Will be"],
        [
            (
                labels[c.action],
                c.record.type,
                c.record.relative(zone),
                _describe(c.current) or "—",
                c.record.value,
            )
            for c in changes
        ],
    )


def apply(client: Client, zone_id: str, changes: list[Change], zone: str) -> bool:
    """Show the changes and apply them after confirmation. Returns True if DNS is as wanted."""
    show(changes, zone)
    conflicts = [c for c in changes if c.action == "conflict"]
    if conflicts:
        ui.warn(
            "Some names have several records of the same type. Fix them by hand in Cloudflare: "
            + ", ".join(c.record.relative(zone) for c in conflicts)
        )
    todo = [c for c in changes if c.needs_write]
    if not todo:
        if not conflicts:
            ui.success("Cloudflare DNS is already correct.")
        return not conflicts
    if not ui.confirm(f"Apply these {len(todo)} DNS change(s) in Cloudflare?", default=False):
        ui.note("No DNS changes made.")
        return False
    for change in todo:
        if change.action == "replace":
            ui.warn(
                f"{change.record.relative(zone)} currently has {_describe(change.current)}, which must be "
                f"removed to add the {change.record.type} record."
            )
            if not ui.confirm(
                f"Delete it and create {change.record.type} {change.record.value}?", default=False
            ):
                ui.note(f"Skipped {change.record.relative(zone)}.")
                continue
            for existing in change.current:
                client.delete(zone_id, existing["id"])
            client.create(zone_id, change.record)
        elif change.action == "update":
            client.update(zone_id, change.current[0]["id"], change.record)
        else:
            client.create(zone_id, change.record)
        ui.success(f"{change.action.capitalize()}d {change.record.type} {change.record.relative(zone)}")
    return True


def check_token(token: str, zone: str) -> str | None:
    """Return an error message, or None if the token can see the zone."""
    try:
        Client(token).zone_id(zone)
    except CloudflareError as exc:
        return str(exc)
    return None
