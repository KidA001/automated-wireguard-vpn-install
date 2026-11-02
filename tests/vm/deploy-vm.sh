#!/usr/bin/env bash
# Runs the real ./deploy.sh against the local test VM, non-interactively.
# Usage: tests/vm/deploy-vm.sh [--fresh] [extra deploy.sh args]
#   --fresh   reset the VM and the test config first (full first-install test)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export VPN_CONFIG_DIR="$HERE/.config"
export VPN_VAULT_PASSWORD="vm-test-vault-password"
export VPN_ROOT_PASSWORD="testroot"
export VPN_SKIP_SSH_CONFIG=1
export VPN_ACCEPT_NEW_HOSTKEY=1

if [[ "${1:-}" == "--fresh" ]]; then
  shift
  "$HERE/test-vm.sh" reset
  rm -rf "$VPN_CONFIG_DIR"
fi

IP="$("$HERE/test-vm.sh" ip)"
LIMA_USER="$(limactl shell --workdir / "${VPN_TEST_VM:-vpn-test}" -- whoami)"
export VPN_EXTRA_VARS="{\"wg_endpoint_override\": \"$IP\", \"ssh_extra_allow_users\": [\"$LIMA_USER\"], \"tls_mode\": \"internal\"}"

mkdir -p "$VPN_CONFIG_DIR"
chmod 700 "$VPN_CONFIG_DIR"
if [[ ! -f "$VPN_CONFIG_DIR/settings.yml" ]]; then
  ssh-keygen -q -t ed25519 -N "" -C vm-test -f "$VPN_CONFIG_DIR/id_ed25519"
  (cd "$REPO" && uv run --quiet python -c "from wizard import sshboot; sshboot.remove_host_key('$IP', 22)")
  cat >"$VPN_CONFIG_DIR/settings.yml" <<EOF
vps_ipv4: "$IP"
admin_user: testadmin
ssh_key_path: "$VPN_CONFIG_DIR/id_ed25519"
zone: example.test
dns_provider: Test
vpn_ns: vpntest
admin_email: admin@example.test
timezone: America/New_York
blocklist_profile: balanced
EOF
fi

exec "$REPO/deploy.sh" --non-interactive --skip-dns-check "$@"
