#!/usr/bin/env bash
# Local test VM that imitates a fresh Debian 13 VPS (root + password SSH).
# Usage: tests/vm/test-vm.sh {golden|create|reset|ip|shell|stop|destroy}
#   golden  build "vpn-golden": a fresh VM with OS updates and our Debian packages already
#           downloaded (not installed). reset/create clone it, which saves several minutes.
set -euo pipefail

NAME="${VPN_TEST_VM:-vpn-test}"
GOLDEN=vpn-golden
PACKAGES="apparmor apparmor-utils ca-certificates chrony curl dnsutils gnupg jq nftables sqlite3"
PACKAGES+=" systemd-zram-generator wireguard-tools unbound dns-root-data fail2ban python3-systemd"
PACKAGES+=" sudo unattended-upgrades oathtool"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

vm_ip() {
  # The vzNAT interface is lima0.
  limactl shell --workdir / "$NAME" -- sh -c \
    "ip -4 -o addr show dev lima0 | awk '{print \$4}' | cut -d/ -f1 | head -1"
}

golden() {
  limactl delete --force "$GOLDEN" 2>/dev/null || true
  limactl create --tty=false --name="$GOLDEN" "$HERE/vps.yaml"
  limactl start --tty=false "$GOLDEN"
  limactl shell --workdir / "$GOLDEN" -- sudo env DEBIAN_FRONTEND=noninteractive sh -c "
    apt-get update -qq && apt-get -y -qq full-upgrade >/dev/null &&
    apt-get -y -qq --download-only install $PACKAGES >/dev/null"
  limactl stop "$GOLDEN"
  echo "Golden image ready."
}

create() {
  if limactl list -q | grep -qx "$GOLDEN"; then
    limactl clone --tty=false "$GOLDEN" "$NAME" >/dev/null
  else
    limactl create --tty=false --name="$NAME" "$HERE/vps.yaml"
  fi
  limactl start --tty=false "$NAME"
  echo "VM ready. IP: $(vm_ip)  (root password: testroot)"
}

destroy() {
  limactl delete --force "$NAME" 2>/dev/null || true
}

case "${1:-}" in
  golden) golden ;;
  create) create ;;
  reset) destroy; create ;;
  ip) vm_ip ;;
  shell) limactl shell --workdir / "$NAME" ;;
  stop) limactl stop "$NAME" ;;
  destroy) destroy ;;
  *) echo "usage: $0 {golden|create|reset|ip|shell|stop|destroy}" >&2; exit 2 ;;
esac
