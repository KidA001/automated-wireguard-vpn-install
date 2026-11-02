import pytest

from wizard import detect, dnsrecords
from wizard import settings as settings_mod


@pytest.mark.parametrize(
    ("ns", "provider"),
    [
        (["ada.ns.cloudflare.com", "bob.ns.cloudflare.com"], "Cloudflare"),
        (["ns-123.awsdns-45.com"], "AWS Route 53"),
        (["dns1.registrar-servers.com"], "Namecheap"),
        (["ns-cloud-a1.googledomains.com"], "Google Cloud DNS / Squarespace"),
        (["ns1.example-unknown.net"], ""),
        ([], ""),
    ],
)
def test_provider_detection(ns, provider):
    assert detect.provider_for(ns) == provider


def test_random_ula_is_valid_and_unique():
    import ipaddress

    a, b = detect.random_ula_cidr(), detect.random_ula_cidr()
    net = ipaddress.IPv6Network(a)
    assert net.prefixlen == 112
    assert net.subnet_of(ipaddress.IPv6Network("fd00::/8"))
    assert a.split("/")[0].endswith("::")
    assert a != b


@pytest.mark.parametrize(
    ("validator", "good", "bad"),
    [
        (detect.v_ipv4, "203.0.113.10", "300.1.1.1"),
        (detect.v_domain, "home.example.net", "example"),
        (detect.v_label, "vpn", "Bad_Label"),
        (detect.v_email, "me@example.com", "me@"),
        (detect.v_time, "02:00", "24:00"),
        (detect.v_port, "51820", "70000"),
        (detect.v_username, "alice", "root"),
        (detect.v_duration, "30d", "30 days"),
        (detect.v_timezone, "America/Chicago", "Mars/Olympus"),
    ],
)
def test_validators(validator, good, bad):
    assert validator(good) is True
    assert validator(bad) is not True


def test_empty_label_allowed_only_when_requested():
    assert detect.v_label("", allow_empty=True) is True
    assert detect.v_label("") is not True


def test_timezone_detection_returns_valid_name():
    name, _ = detect.timezone()
    assert detect.valid_timezone(name)


def _settings(**overrides):
    s = {**settings_mod.DEFAULTS, "zone": "example.com", **overrides}
    s["labels"] = dict(settings_mod.DEFAULTS["labels"])
    return s


def test_hostnames_with_and_without_namespace():
    assert settings_mod.hostnames(_settings())["wg"] == "wg.vpn.example.com"
    assert settings_mod.hostnames(_settings(vpn_ns=""))["auth"] == "auth.example.com"
    assert settings_mod.vpn_host(_settings(vpn_ns="")) == "example.com"


def test_required_records():
    records = dnsrecords.required(_settings(), "203.0.113.10", "2001:db8::10")
    by_type = {(r.type, r.relative("example.com")): r.value for r in records}
    assert by_type[("A", "vpn")] == "203.0.113.10"
    assert by_type[("AAAA", "vpn")] == "2001:db8::10"
    assert by_type[("CNAME", "auth.vpn")] == "vpn.example.com"
    assert len(records) == 6


def test_records_without_ipv6_or_namespace():
    records = dnsrecords.required(_settings(vpn_ns=""), "203.0.113.10", None)
    assert [r.type for r in records].count("AAAA") == 0
    assert records[0].relative("example.com") == "@"


def test_remove_unmanaged_host_stanza():
    from wizard.sshboot import _remove_unmanaged_host

    text = (
        "Host other\n  User a\n\n"
        "Host vpn\n  User bob\n  HostName 1.2.3.4\n\n"
        "Host vpn2\n  User c\n"
        "# >>> wireguard-vpn-server vpn\nHost vpn\n  User alice\n# <<< wireguard-vpn-server vpn\n"
    )
    out, removed = _remove_unmanaged_host(
        text, "vpn", "# >>> wireguard-vpn-server vpn", "# <<< wireguard-vpn-server vpn"
    )
    assert removed
    assert "bob" not in out
    assert "Host other" in out and "Host vpn2" in out
    assert "User alice" in out


def test_remove_host_key_survives_malformed_lines(tmp_path, monkeypatch):
    from wizard import sshboot

    kh = tmp_path / "known_hosts"
    kh.write_text(
        "good.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIASu+4NzmLFhWYMy3ZhfxgyF1fPEbKqndoqKuvTBGmJ\n"
        "broken.example ssh-ed25518 AAAAC3NzaC1lZDI1NTE5AAAAIASu+4NzmLFhWYMy3ZhfxgyF1fPEbKqndoqKuvTBGmJ\n"
        "203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGYhaWTNomA9Cud8LG1wKaLQD9a4VT3zxHatScysyJyC\n"
    )
    monkeypatch.setattr(sshboot, "KNOWN_HOSTS", kh)
    sshboot.remove_host_key("203.0.113.10", 22)
    text = kh.read_text()
    assert "203.0.113.10" not in text
    assert "broken.example" in text and "good.example" in text
