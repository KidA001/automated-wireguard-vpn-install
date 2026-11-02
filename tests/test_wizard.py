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
    assert len(records) == 5


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


def test_pin_extra_port_copies_trusted_key(tmp_path, monkeypatch):
    from wizard import sshboot

    kh = tmp_path / "known_hosts"
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGYhaWTNomA9Cud8LG1wKaLQD9a4VT3zxHatScysyJyC"
    kh.write_text(f"203.0.113.10 {key}\n")
    monkeypatch.setattr(sshboot, "KNOWN_HOSTS", kh)
    sshboot.pin_extra_port("203.0.113.10", 22, 2222)
    sshboot.pin_extra_port("203.0.113.10", 22, 2222)  # idempotent
    assert kh.read_text().count(f"[203.0.113.10]:2222 {key}") == 1
    sshboot.pin_extra_port("203.0.113.10", 22, 22)
    assert kh.read_text().count(key) == 2


class _FakeSSH:
    """Simulates a server's sudo password for apply_pending_sudo_password."""

    def __init__(self, server_password, chpasswd_ok=True):
        self.server_password = server_password
        self.chpasswd_ok = chpasswd_ok

    def sudo_works(self, ip, port, user, key, password):
        return password == self.server_password

    def change_password(self, ip, port, user, key, current, new):
        if current != self.server_password or not self.chpasswd_ok:
            return False
        self.server_password = new
        return True


def _pending_setup(monkeypatch, fake):
    from wizard import cli, secrets_store, sshboot

    monkeypatch.setattr(sshboot, "sudo_works", fake.sudo_works)
    monkeypatch.setattr(sshboot, "change_password", fake.change_password)
    saved = {}
    monkeypatch.setattr(secrets_store, "save", lambda pw, data: saved.update(data))
    settings = {"vps_ipv4": "203.0.113.10", "ssh_port_active": 22, "admin_user": "u", "ssh_key_path": "/k"}
    return cli, settings, saved


def test_pending_sudo_password_is_applied(monkeypatch):
    fake = _FakeSSH("old")
    cli, settings, saved = _pending_setup(monkeypatch, fake)
    vault = {"vault_sudo_password": "old", "vault_sudo_password_pending": "new"}
    cli.apply_pending_sudo_password(settings, vault, "vaultpw")
    assert fake.server_password == "new"
    assert vault == {"vault_sudo_password": "new"}
    assert saved == vault


def test_pending_sudo_password_keeps_old_on_failure(monkeypatch):
    import pytest

    fake = _FakeSSH("old", chpasswd_ok=False)
    cli, settings, saved = _pending_setup(monkeypatch, fake)
    vault = {"vault_sudo_password": "old", "vault_sudo_password_pending": "new"}
    with pytest.raises(SystemExit):
        cli.apply_pending_sudo_password(settings, vault, "vaultpw")
    assert fake.server_password == "old"
    assert vault["vault_sudo_password"] == "old"
    assert vault["vault_sudo_password_pending"] == "new"
    assert saved == {}


def test_pending_sudo_password_already_applied(monkeypatch):
    # e.g. a previous run changed it on the server but was interrupted before saving the vault
    fake = _FakeSSH("new")
    cli, settings, _ = _pending_setup(monkeypatch, fake)
    vault = {"vault_sudo_password": "old", "vault_sudo_password_pending": "new"}
    cli.apply_pending_sudo_password(settings, vault, "vaultpw")
    assert vault == {"vault_sudo_password": "new"}


def test_check_mode_never_bootstraps(monkeypatch, tmp_path):
    import pytest

    from wizard import cli, runner, secrets_store
    from wizard import settings as settings_mod

    settings = {**settings_mod.DEFAULTS, "vps_ipv4": "203.0.113.10", "admin_user": "u", "zone": "example.com",
                "ssh_key_path": "/k", "timezone": "Etc/UTC", "wg_ipv6_cidr": "fd00:1::/112"}  # fmt: skip
    monkeypatch.setattr(secrets_store, "vault_password", lambda: "pw")
    monkeypatch.setattr(secrets_store, "load", lambda pw: {"vault_sudo_password": "x"})
    monkeypatch.setattr(secrets_store, "save", lambda pw, data: None)
    monkeypatch.setattr(settings_mod, "load", lambda: dict(settings))
    monkeypatch.setattr(settings_mod, "save", lambda s: None)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "exists")
    (tmp_path / "exists").write_text("x")
    monkeypatch.setattr(runner, "install_collections", lambda: None)
    monkeypatch.setattr(cli, "admin_login_works", lambda s: False)
    monkeypatch.setattr(cli, "bootstrap", lambda *a: pytest.fail("bootstrap must not run in --check"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--check", "--non-interactive"])
    assert "Nothing was changed" in str(exc.value)


def test_secret_hashes_are_stable_and_verifiable():
    from wizard import secrets_store as s

    vault = {"vault_authelia_password": "correct horse battery"}
    s.ensure_generated(vault)
    first = dict(vault)
    s.ensure_generated(vault)
    assert vault == first  # idempotent: no new salts on re-runs
    assert vault["vault_authelia_password_hash"].startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert s.argon2_matches("correct horse battery", vault["vault_authelia_password_hash"])
    assert vault["vault_oidc_client_secret_hash"].startswith("$pbkdf2-sha512$310000$")
    assert s.pbkdf2_matches(vault["vault_oidc_client_secret"], vault["vault_oidc_client_secret_hash"])
    assert "-----BEGIN PRIVATE KEY-----" in vault["vault_oidc_jwk"]

    vault["vault_authelia_password"] = "a different password"
    s.ensure_generated(vault)
    assert vault["vault_authelia_password_hash"] != first["vault_authelia_password_hash"]
    assert vault["vault_oidc_client_secret_hash"] == first["vault_oidc_client_secret_hash"]


def test_pbkdf2_matches_known_vector():
    import base64
    import hashlib

    from wizard import secrets_store as s

    salt = b"0123456789abcdef"
    encoded = s.pbkdf2_sha512("secret", salt, 1000)
    digest = hashlib.pbkdf2_hmac("sha512", b"secret", salt, 1000)
    assert encoded == "$pbkdf2-sha512$1000$" + "$".join(
        base64.b64encode(x).decode().rstrip("=").replace("+", ".") for x in (salt, digest)
    )
    assert s.pbkdf2_matches("secret", encoded)
    assert not s.pbkdf2_matches("secret", "garbage")


def test_totp_secret_and_uri():
    import base64

    from wizard import cli, secrets_store

    secret = secrets_store.totp_secret()
    assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20
    uri = cli.totp_uri({"vpn_ns": "vpn", "zone": "example.com", "admin_user": "alice"}, secret)
    assert uri.startswith("otpauth://totp/vpn.example.com:alice?secret=")
    assert "issuer=vpn.example.com" in uri and "period=30" in uri


def test_identity_dump_roundtrip_and_device_count():
    from wizard import state

    dump = (
        "CREATE TABLE clients_table (id INTEGER PRIMARY KEY, name TEXT);\n"
        "INSERT INTO clients_table VALUES(1,'phone');\nINSERT INTO clients_table VALUES(2,'laptop');\n"
    )
    blob = state.encode_dump(dump)
    assert state.encode_dump(dump) == blob  # deterministic (mtime=0): no vault churn
    assert state.decode_dump(blob) == dump
    assert state.device_count(dump) == 2
    assert state.device_count("garbage") == 0


def test_backup_keys_encrypt_and_decrypt(tmp_path, monkeypatch):
    import pyrage

    from wizard import state

    vault = {}
    state.ensure_backup_keys(vault)
    keys = dict(vault)
    state.ensure_backup_keys(vault)
    assert vault == keys  # never rotated
    recipient = pyrage.x25519.Recipient.from_str(vault["vault_backup_recipient"])
    encrypted = tmp_path / "b.tar.gz.age"
    encrypted.write_bytes(pyrage.encrypt(b"tar bytes", [recipient]))
    monkeypatch.setattr(state, "CONFIG_DIR", tmp_path)
    plain = state.backup_restore_file(encrypted, vault)
    assert plain.read_bytes() == b"tar bytes"
    assert oct(plain.stat().st_mode & 0o777) == "0o600"


def test_ula_prefix_is_canonical():
    assert detect.canonical_cidr("fd63:e013:012b::/112") == "fd63:e013:12b::/112"
    for _ in range(50):
        cidr = detect.random_ula_cidr()
        assert cidr == detect.canonical_cidr(cidr)


def test_every_prompt_runs(monkeypatch):
    """Walk through every essential and advanced prompt with canned answers (catches crashes)."""
    from wizard import cloudflare, prompts, ui
    from wizard import settings as settings_mod

    answers = {
        "VPS public IPv4 address": "203.0.113.10",
        "Admin username on the server": "alice",
        "Your domain (the zone you manage at your DNS provider)": "example.com",
        "VPN namespace label (empty = put hosts directly under the domain)": "vpn",
    }
    monkeypatch.setattr(ui, "NON_INTERACTIVE", False)
    monkeypatch.setattr(
        ui, "text", lambda prompt, default="", validate=None, help_text="": answers.get(prompt, default)
    )
    monkeypatch.setattr(ui, "confirm", lambda prompt, default=True, help_text="": default)
    monkeypatch.setattr(ui, "select", lambda prompt, choices, default, help_text="": default)
    monkeypatch.setattr(ui, "secret", lambda *a, **k: "correct horse battery staple")
    monkeypatch.setattr(prompts.detect, "nameservers", lambda zone: ["ada.ns.cloudflare.com"])
    monkeypatch.setattr(cloudflare, "check_token", lambda token, zone: None)
    settings = {**settings_mod.DEFAULTS, "labels": dict(settings_mod.DEFAULTS["labels"])}
    vault = {}
    prompts.essentials(settings, vault, need_ssh=False)
    monkeypatch.setattr(ui, "confirm", lambda prompt, default=True, help_text="": True)  # walk all advanced
    prompts.advanced(settings)
    assert settings["zone"] == "example.com" and settings["dns_provider"] == "Cloudflare"
    assert settings["admin_email"] == "alice@example.com"  # sensible default, no real mailbox needed
    assert vault["vault_sudo_password"] and vault["vault_authelia_password"]
