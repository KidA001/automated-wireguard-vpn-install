#!/usr/bin/env bash
# Local test VM that imitates a fresh Debian 13 VPS (root + password SSH).
# Usage: tests/vm/test-vm.sh {create|reset|ip|shell|stop|destroy}
set -euo pipefail

NAME="${VPN_TEST_VM:-vpn-test}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

vm_ip() {
  # The vzNAT interface is lima0.
  limactl shell --workdir / "$NAME" -- sh -c \
    "ip -4 -o addr show dev lima0 | awk '{print \$4}' | cut -d/ -f1 | head -1"
}

create() {
  limactl create --tty=false --name="$NAME" "$HERE/vps.yaml"
  limactl start --tty=false "$NAME"
  echo "VM ready. IP: $(vm_ip)  (root password: testroot)"
}

destroy() {
  limactl delete --force "$NAME" 2>/dev/null || true
}

case "${1:-}" in
  create) create ;;
  reset) destroy; create ;;
  ip) vm_ip ;;
  shell) limactl shell --workdir / "$NAME" ;;
  stop) limactl stop "$NAME" ;;
  destroy) destroy ;;
  *) echo "usage: $0 {create|reset|ip|shell|stop|destroy}" >&2; exit 2 ;;
esac
