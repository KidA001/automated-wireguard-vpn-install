import json

import httpx
import pytest

from wizard import cloudflare, ui
from wizard.dnsrecords import Record

ZONE = "example.com"


class FakeCloudflare:
    """Just enough of the Cloudflare v4 API for the wizard."""

    def __init__(self, records):
        self.records = [dict(r, id=str(i)) for i, r in enumerate(records)]
        self.writes = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.endswith("/zones"):
            result = [{"id": "z1"}] if request.url.params["name"] == ZONE else []
        elif method == "GET":
            result = [r for r in self.records if r["name"] == request.url.params["name"]]
        elif method == "POST":
            body = json.loads(request.content)
            self.records.append(dict(body, id=str(len(self.records) + 100)))
            self.writes.append(("create", body["type"], body["name"]))
            result = {}
        elif method == "PUT":
            body = json.loads(request.content)
            rid = path.rsplit("/", 1)[1]
            self.records = [dict(body, id=rid) if r["id"] == rid else r for r in self.records]
            self.writes.append(("update", body["type"], body["name"]))
            result = {}
        elif method == "DELETE":
            rid = path.rsplit("/", 1)[1]
            self.records = [r for r in self.records if r["id"] != rid]
            self.writes.append(("delete", rid))
            result = {}
        return httpx.Response(200, json={"success": True, "result": result, "errors": []})


WANTED = [
    Record("A", "vpn.example.com", "203.0.113.10"),
    Record("CNAME", "auth.vpn.example.com", "vpn.example.com"),
    Record("CNAME", "wg.vpn.example.com", "vpn.example.com"),
    Record("CNAME", "adguard.vpn.example.com", "vpn.example.com"),
]


def _client(fake):
    return cloudflare.Client("token", transport=httpx.MockTransport(fake))


def test_plan_actions():
    fake = FakeCloudflare(
        [
            {"type": "A", "name": "vpn.example.com", "content": "198.51.100.1", "proxied": False},
            {"type": "CNAME", "name": "auth.vpn.example.com", "content": "vpn.example.com", "proxied": False},
            {"type": "A", "name": "wg.vpn.example.com", "content": "198.51.100.1", "proxied": True},
            {"type": "TXT", "name": "adguard.vpn.example.com", "content": "keep me"},
        ]
    )
    client = _client(fake)
    actions = {c.record.name: c.action for c in cloudflare.plan(client, "z1", WANTED)}
    assert actions == {
        "vpn.example.com": "update",
        "auth.vpn.example.com": "unchanged",
        "wg.vpn.example.com": "replace",
        "adguard.vpn.example.com": "create",
    }


def test_proxied_record_is_updated_to_dns_only():
    fake = FakeCloudflare(
        [{"type": "A", "name": "vpn.example.com", "content": "203.0.113.10", "proxied": True}]
    )
    changes = cloudflare.plan(_client(fake), "z1", WANTED[:1])
    assert changes[0].action == "update"


def test_duplicate_records_are_a_conflict_and_never_touched(monkeypatch):
    fake = FakeCloudflare(
        [
            {"type": "A", "name": "vpn.example.com", "content": "198.51.100.1"},
            {"type": "A", "name": "vpn.example.com", "content": "198.51.100.2"},
        ]
    )
    client = _client(fake)
    changes = cloudflare.plan(client, "z1", WANTED[:1])
    assert changes[0].action == "conflict"
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("nothing to confirm"))
    assert cloudflare.apply(client, "z1", changes, ZONE) is False
    assert fake.writes == []


def test_apply_needs_confirmation(monkeypatch):
    fake = FakeCloudflare([])
    client = _client(fake)
    changes = cloudflare.plan(client, "z1", WANTED)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    assert cloudflare.apply(client, "z1", changes, ZONE) is False
    assert fake.writes == []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    assert cloudflare.apply(client, "z1", changes, ZONE) is True
    assert sorted(w[0] for w in fake.writes) == ["create"] * 4
    assert all(r["proxied"] is False and r["comment"] == cloudflare.COMMENT for r in fake.records)


def test_replace_asks_separately_before_deleting(monkeypatch):
    fake = FakeCloudflare([{"type": "A", "name": "wg.vpn.example.com", "content": "198.51.100.1"}])
    client = _client(fake)
    changes = cloudflare.plan(client, "z1", [WANTED[2]])
    answers = iter([True, False])  # apply: yes; delete the conflicting A record: no
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: next(answers))
    cloudflare.apply(client, "z1", changes, ZONE)
    assert fake.writes == []


def test_token_without_zone_access():
    fake = FakeCloudflare([])
    with pytest.raises(cloudflare.CloudflareError, match="can't see the zone"):
        _client(fake).zone_id("other.org")
