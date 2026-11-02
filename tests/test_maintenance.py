import importlib.util
from pathlib import Path

import pytest

_path = Path(__file__).parents[1] / "ansible/roles/maintenance/files/vpn_maintenance.py"
_spec = importlib.util.spec_from_file_location("vpn_maintenance", _path)
vm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vm)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("caddy:2", ("caddy", "2")),
        ("ghcr.io/wg-easy/wg-easy:15", ("ghcr.io/wg-easy/wg-easy", "15")),
        ("localhost:5000/caddy:2", ("localhost:5000/caddy", "2")),
        ("localhost:5000/caddy", ("localhost:5000/caddy", "latest")),
        ("adguard/adguardhome:v0.107.*", ("adguard/adguardhome", "v0.107.*")),
    ],
)
def test_split_ref(ref, expected):
    assert vm.split_ref(ref) == expected


def test_pick_newest_ignores_prereleases_and_other_series():
    tags = ["v0.107.9", "v0.107.79", "v0.107.78", "v0.107.80-b.1", "v0.108.0", "latest", "v1.0.0-b.1"]
    assert vm.pick_newest(tags, "v0.107.") == "v0.107.79"
    assert vm.pick_newest(tags, "v0.109.") is None


@pytest.mark.parametrize(
    ("repo", "path"),
    [
        ("caddy", "library/caddy"),
        ("adguard/adguardhome", "adguard/adguardhome"),
        ("docker.io/caddy", "library/caddy"),
        ("ghcr.io/wg-easy/wg-easy", None),
        ("localhost:5000/caddy", None),
    ],
)
def test_hub_repo(repo, path):
    assert vm.hub_repo(repo) == path


def test_digest_ref_matches_repository():
    digests = ["other@sha256:aaa", "caddy@sha256:bbb"]
    assert vm.digest_ref("caddy:2", digests) == "caddy@sha256:bbb"
    assert vm.digest_ref("docker.io/library/caddy:2", digests) == "caddy@sha256:bbb"
    with pytest.raises(vm.StepError):
        vm.digest_ref("ghcr.io/x/y:1", digests)


def test_resolve_wildcard(monkeypatch):
    monkeypatch.setattr(vm, "hub_tags", lambda repo, f="": ["v0.107.78", "v0.107.79", "latest"])
    assert vm.resolve("adguard/adguardhome:v0.107.*") == "adguard/adguardhome:v0.107.79"
    assert vm.resolve("caddy:2") == "caddy:2"


def _cfg(tmp_path):
    return {
        "root": str(tmp_path),
        "health_timeout": 5,
        "services": {"caddy": {"env": "IMAGE_CADDY", "track": "caddy:2"}},
    }


def test_rollback_quarantines_unhealthy_digest(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    pins = vm.Pins(tmp_path, cfg["services"])
    pins.pinned["caddy"] = {"track": "caddy:2", "tag": "2", "image": "caddy@sha256:good", "since": "x"}
    pins.save()
    monkeypatch.setattr(vm, "pull", lambda ref: "caddy@sha256:bad")
    ups = []
    monkeypatch.setattr(vm, "compose_up", lambda root, svc, t: ups.append(svc) or len(ups) > 1)
    monkeypatch.setattr(vm, "run", lambda *a, **k: None)
    report = {"updated": [], "rolled_back": [], "skipped": [], "errors": []}
    vm.update_containers(cfg, report)
    pins = vm.Pins(tmp_path, cfg["services"])
    assert pins.pinned["caddy"]["image"] == "caddy@sha256:good"
    assert pins.quarantine == {"caddy": ["sha256:bad"]}
    assert "IMAGE_CADDY=caddy@sha256:good" in (tmp_path / ".env").read_text()
    assert report["rolled_back"] and "restored" in report["rolled_back"][0]

    # The same bad digest is skipped next time, without touching the container.
    report = {"updated": [], "rolled_back": [], "skipped": [], "errors": []}
    ups.clear()
    vm.update_containers(cfg, report)
    assert ups == [] and report["skipped"]


def test_healthy_update_is_recorded(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    pins = vm.Pins(tmp_path, cfg["services"])
    pins.pinned["caddy"] = {"track": "caddy:2", "tag": "2", "image": "caddy@sha256:old", "since": "x"}
    pins.save()
    monkeypatch.setattr(vm, "pull", lambda ref: "caddy@sha256:new")
    monkeypatch.setattr(vm, "compose_up", lambda root, svc, t: True)
    monkeypatch.setattr(vm, "run", lambda *a, **k: None)
    report = {"updated": [], "rolled_back": [], "skipped": [], "errors": []}
    vm.update_containers(cfg, report)
    assert vm.Pins(tmp_path, cfg["services"]).pinned["caddy"]["image"] == "caddy@sha256:new"
    assert report["updated"] == ["caddy 2 -> 2"]
