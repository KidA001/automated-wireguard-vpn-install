#!/usr/bin/python3
"""vpn-maintenance: nightly OS + container updates for the VPN server (PLAN.md §11).

Steps run in order, never overlapping (a lock is shared with vpn-heal):
  1. OS packages (unattended-upgrade: Debian, Debian security, Docker repo)
  2. Containers, one at a time: pull the tracked tag; if its digest is new, switch to it and
     wait for the healthcheck. An unhealthy update is rolled back and that digest is
     quarantined (never retried; the next different digest is tried normally).
  3. Weekly (Sundays): report newer major versions (never applied automatically)
  4. Optional Healthchecks.io ping (start / success / fail), sent before any reboot
  5. Reboot if an update requires it (and auto-reboot is on)

Other modes:
  --sync     used by ./deploy.sh: pin any service whose tracked tag changed or that isn't
             pinned yet (no other updates). Prints JSON for Ansible.
  --status   print a human summary of the last run.

Pinned image digests live in <root>/images.json and are exported to <root>/.env, which
docker compose reads (IMAGE_* variables). Config: /etc/vpn/maintenance.json.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CONFIG_FILE = Path("/etc/vpn/maintenance.json")
STATE_FILE = Path("/var/lib/vpn/maintenance.json")
LOCK_FILE = Path("/run/vpn-maintenance.lock")
# Containers are updated in this order; wg-easy last because it briefly drops the tunnel.
ORDER = ["authelia", "caddy", "adguard", "wg-easy"]


class StepError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def run(cmd: list[str], check: bool = True, timeout: int = 1800, cwd: str | None = None):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if check and result.returncode != 0:
        raise StepError(f"{' '.join(cmd[:4])}…: {(result.stderr or result.stdout).strip()[-300:]}")
    return result


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    tmp.replace(path)


# --- image references -----------------------------------------------------------------


def split_ref(ref: str) -> tuple[str, str]:
    """'ghcr.io/a/b:15' -> ('ghcr.io/a/b', '15'). A ':' after the last '/' is the tag."""
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :]
    return ref, "latest"


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", tag))


def pick_newest(tags: list[str], prefix: str) -> str | None:
    """Newest stable tag that starts with prefix, e.g. prefix 'v0.107.' -> 'v0.107.79'."""
    pattern = re.compile(re.escape(prefix) + r"\d+(\.\d+)*$")
    matches = [t for t in tags if pattern.match(t)]
    return max(matches, key=version_key) if matches else None


def hub_repo(repo: str) -> str | None:
    """Docker Hub repository path, or None for other registries."""
    parts = repo.split("/")
    if "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        if parts[0] != "docker.io":
            return None
        parts = parts[1:]
    return "/".join(parts) if len(parts) > 1 else f"library/{parts[0]}"


def hub_tags(repo: str, name_filter: str = "") -> list[str]:
    path = hub_repo(repo)
    if path is None:
        raise StepError(f"tag listing is only supported for Docker Hub images ({repo})")
    url = f"https://hub.docker.com/v2/repositories/{path}/tags?page_size=100&name={name_filter}"
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
        return [t["name"] for t in json.load(resp)["results"]]


def resolve(track: str) -> str:
    """'repo:tag' stays as is; 'repo:prefix*' becomes the newest matching tag."""
    repo, tag = split_ref(track)
    if not tag.endswith("*"):
        return track
    prefix = tag[:-1]
    newest = pick_newest(hub_tags(repo, prefix), prefix)
    if not newest:
        raise StepError(f"no tag matches {track}")
    return f"{repo}:{newest}"


def digest_ref(ref: str, repo_digests: list[str]) -> str:
    """Pick the RepoDigest that belongs to ref's repository (docker.io names are shortened)."""
    repo, _ = split_ref(ref)
    names = {repo, repo.removeprefix("docker.io/"), repo.removeprefix("docker.io/library/")}
    for entry in repo_digests:
        name, _, digest = entry.partition("@")
        if name in names and digest:
            return entry
    raise StepError(f"no registry digest for {ref}")


def pull(ref: str) -> str:
    run(["docker", "pull", "-q", ref], timeout=900)
    out = run(["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"])
    return digest_ref(ref, json.loads(out.stdout))


# --- pinned images ----------------------------------------------------------------------


class Pins:
    def __init__(self, root: Path, services: dict[str, dict]):
        self.root = root
        self.services = services
        self.path = root / "images.json"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.pinned: dict[str, dict] = data.get("pinned", {})
        self.quarantine: dict[str, list[str]] = data.get("quarantine", {})

    def save(self) -> None:
        data = {"pinned": self.pinned, "quarantine": self.quarantine}
        write_atomic(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")
        env = "".join(
            f"{cfg['env']}={self.pinned[name]['image']}\n"
            for name, cfg in sorted(self.services.items())
            if name in self.pinned
        )
        write_atomic(self.root / ".env", "# Written by vpn-maintenance: pinned image digests.\n" + env)

    def images(self) -> dict[str, str]:
        return {name: p["image"] for name, p in self.pinned.items()}


def compose_up(root: Path, service: str, timeout: int) -> bool:
    result = run(
        ["docker", "compose", "up", "-d", "--wait", "--wait-timeout", str(timeout), service],
        check=False,
        timeout=timeout + 120,
        cwd=str(root),
    )
    return result.returncode == 0


def sync(cfg: dict) -> dict:
    """Pin services that aren't pinned yet or whose tracked tag changed. No other updates."""
    root = Path(cfg["root"])
    pins = Pins(root, cfg["services"])
    changed = []
    for name, svc in cfg["services"].items():
        current = pins.pinned.get(name)
        if current and current.get("track") == svc["track"]:
            if run(["docker", "image", "inspect", current["image"]], check=False).returncode != 0:
                run(["docker", "pull", "-q", current["image"]], timeout=900)
            continue
        ref = resolve(svc["track"])
        pins.pinned[name] = {
            "track": svc["track"],
            "tag": split_ref(ref)[1],
            "image": pull(ref),
            "since": now(),
        }
        pins.quarantine.pop(name, None)
        changed.append(name)
    if changed or not (root / ".env").exists():
        pins.save()
    return {"changed": bool(changed), "updated": changed, "images": pins.images()}


def update_containers(cfg: dict, report: dict) -> None:
    root = Path(cfg["root"])
    pins = Pins(root, cfg["services"])
    timeout = int(cfg.get("health_timeout", 300))
    for name in sorted(cfg["services"], key=lambda n: ORDER.index(n) if n in ORDER else len(ORDER)):
        svc, current = cfg["services"][name], pins.pinned.get(name)
        if not current:
            report["errors"].append(f"{name}: not pinned (run ./deploy.sh)")
            continue
        try:
            ref = resolve(svc["track"])
            candidate = pull(ref)
        except (StepError, OSError, subprocess.TimeoutExpired) as exc:
            report["errors"].append(f"{name}: {exc}")
            continue
        if candidate == current["image"]:
            continue
        digest = candidate.split("@", 1)[1]
        if digest in pins.quarantine.get(name, []):
            report["skipped"].append(f"{name} {split_ref(ref)[1]} (quarantined)")
            continue
        previous = dict(current)
        pins.pinned[name] = {**current, "image": candidate, "tag": split_ref(ref)[1], "since": now()}
        pins.save()
        if compose_up(root, name, timeout):
            report["updated"].append(f"{name} {previous['tag']} -> {split_ref(ref)[1]}")
            continue
        # Unhealthy: go back to the last good image and never retry this digest.
        pins.pinned[name] = previous
        pins.quarantine.setdefault(name, []).append(digest)
        pins.save()
        restored = compose_up(root, name, timeout)
        report["rolled_back"].append(
            f"{name} {split_ref(ref)[1]} ({digest[:19]}) was unhealthy; "
            + ("restored the previous version" if restored else "RESTORE FAILED")
        )
    run(["docker", "image", "prune", "-f"], check=False)


def check_majors(cfg: dict) -> list[str]:
    found = []
    for name, svc in cfg["services"].items():
        repo, tag = split_ref(svc["track"])
        try:
            if tag.endswith("*"):
                prefix = tag[:-1]
                parts = version_key(prefix)
                stable = [t for t in hub_tags(repo) if re.fullmatch(r"v?\d+\.\d+\.\d+", t)]
                newer = [t for t in stable if version_key(t)[: len(parts)] > parts]
                if newer:
                    found.append(f"{name}: {max(newer, key=version_key)} (tracking {tag})")
            elif tag.isdigit():
                nxt = f"{repo}:{int(tag) + 1}"
                if run(["docker", "manifest", "inspect", nxt], check=False, timeout=60).returncode == 0:
                    found.append(f"{name}: {nxt} (tracking {tag})")
        except (StepError, OSError, subprocess.TimeoutExpired) as exc:
            found.append(f"{name}: check failed ({exc})")
    return found


def os_updates(report: dict) -> None:
    before = run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n"]).stdout.splitlines()
    run(["apt-get", "update", "-qq"], timeout=600)
    run(["unattended-upgrade"], timeout=3600)
    after = run(["dpkg-query", "-W", "-f", "${Package} ${Version}\n"]).stdout.splitlines()
    report["os_packages"] = len(set(after) - set(before))


def reboot_required() -> bool:
    if Path("/run/reboot-required").exists():
        return True
    kernels = run(
        ["sh", "-c", "linux-version list | linux-version sort | tail -1"], check=False
    ).stdout.strip()
    return bool(kernels) and kernels != os.uname().release


def ping(url: str, kind: str, body: str = "") -> None:
    if not url:
        return
    target = url.rstrip("/") + ({"start": "/start", "fail": "/fail"}.get(kind, ""))
    for _ in range(3):
        try:
            req = urllib.request.Request(target, data=body.encode()[:10000], method="POST")  # noqa: S310
            urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 - user's https URL
            return
        except OSError:
            continue


def summary(report: dict) -> str:
    lines = [f"result: {report['result']}", f"OS packages upgraded: {report.get('os_packages', 0)}"]
    for key, label in [
        ("updated", "updated"),
        ("rolled_back", "ROLLED BACK"),
        ("skipped", "skipped"),
        ("majors", "new major version available"),
        ("errors", "ERROR"),
    ]:
        lines += [f"{label}: {item}" for item in report.get(key, [])]
    if report.get("reboot"):
        lines.append(f"reboot: {report['reboot']}")
    return "\n".join(lines)


def maintenance(cfg: dict, args: argparse.Namespace) -> int:
    url = cfg.get("healthchecks_url", "")
    report: dict = {
        "started": now(),
        "updated": [],
        "rolled_back": [],
        "skipped": [],
        "errors": [],
        "majors": [],
    }
    ping(url, "start")
    if not args.skip_os:
        try:
            os_updates(report)
        except (StepError, subprocess.TimeoutExpired) as exc:
            report["errors"].append(f"os: {exc}")
    if not args.skip_containers:
        update_containers(cfg, report)
    if args.check_majors or dt.date.today().weekday() == 6:
        report["majors"] = check_majors(cfg)
    elif STATE_FILE.exists():
        report["majors"] = json.loads(STATE_FILE.read_text()).get("majors", [])
    needs_reboot = reboot_required()
    report["result"] = "failed" if report["errors"] or report["rolled_back"] else "ok"
    if needs_reboot:
        report["reboot"] = (
            "now" if cfg.get("auto_reboot") and not args.no_reboot else "required (auto-reboot off)"
        )
    report["finished"] = now()
    write_atomic(STATE_FILE, json.dumps(report, indent=2) + "\n", 0o644)
    ping(url, "fail" if report["result"] == "failed" else "ok", summary(report))
    print(summary(report))
    if report.get("reboot") == "now":
        subprocess.run(["systemctl", "reboot"], check=False)
    return 1 if report["result"] == "failed" else 0


def status() -> int:
    if not STATE_FILE.exists():
        print("No maintenance run yet.")
        return 0
    report = json.loads(STATE_FILE.read_text())
    print(f"last run: {report.get('started')} → {report.get('finished')}")
    print(summary(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vpn-maintenance", description=__doc__.split("\n")[0])
    p.add_argument("--sync", action="store_true", help="pin new/changed services only (used by deploy)")
    p.add_argument("--status", action="store_true", help="show the last run")
    p.add_argument("--skip-os", action="store_true")
    p.add_argument("--skip-containers", action="store_true")
    p.add_argument("--check-majors", action="store_true", help="check for new major versions now")
    p.add_argument("--no-reboot", action="store_true")
    p.add_argument("--config", type=Path, default=CONFIG_FILE)
    args = p.parse_args(argv)
    if args.status:
        return status()
    cfg = json.loads(args.config.read_text())
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if args.sync else fcntl.LOCK_NB))
        except BlockingIOError:
            print("vpn-maintenance is already running.")
            return 0
        if args.sync:
            print(json.dumps(sync(cfg)))
            return 0
        return maintenance(cfg, args)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
