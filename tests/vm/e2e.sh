#!/usr/bin/env bash
# One-command end-to-end test against the local VMs (see README "Development & testing").
#   tests/vm/e2e.sh          fresh install (from the golden image) + every check
#   tests/vm/e2e.sh --keep   reuse the current VM (skip the fresh install)
#   add --rebuild-tests      also rebuild the VM twice to test identity and backup restores (+15 min)
# Prints a PASS/FAIL summary; full logs go to tests/vm/.logs/.
# Single-quoted commands are expanded inside the VM on purpose.
# shellcheck disable=SC2016
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
LOGS="$HERE/.logs"
VM="${VPN_TEST_VM:-vpn-test}"
D=vpntest.example.test
export VPN_CONFIG_DIR="$HERE/.config" VPN_VAULT_PASSWORD="vm-test-vault-password"
mkdir -p "$LOGS"
RESULTS=()
FAILED=0
START=$(date +%s)

result() { RESULTS+=("$1  $2"); [[ $1 == FAIL ]] && FAILED=1; printf '%s  %s\n' "$1" "$2"; }
check() { local name="$1"; shift; if "$@" >>"$LOGS/checks.log" 2>&1; then result PASS "$name"; else result FAIL "$name"; fi; }
vm() { limactl shell --workdir / "$VM" -- sudo bash -c "$1"; }
secret() { (cd "$REPO" && uv run --quiet python -m wizard --show-secret "$1"); }
timed_deploy() {
  local label="$1" log="$2"; shift 2
  local t0=$(( $(date +%s) ))
  "$HERE/deploy-vm.sh" "$@" >"$log" 2>&1
  local rc=$?
  echo "$(( $(date +%s) - t0 ))s" >"$log.time"
  [[ $rc -eq 0 ]] || { result FAIL "$label (exit $rc, see $log)"; return 1; }
  result PASS "$label ($(cat "$log.time"), $(grep -o 'changed=[0-9]*' "$log" | tail -1))"
}

# Healthchecks pings go to a tiny listener inside the VM.
export VPN_EXTRA_VARS_E2E='"healthchecks_enabled": true, "vault_healthchecks_url": "http://127.0.0.1:8098/hc"'

REBUILD_TESTS=0
[[ " $* " == *" --rebuild-tests "* ]] && REBUILD_TESTS=1
if [[ " $* " != *" --keep "* ]]; then
  timed_deploy "fresh install" "$LOGS/deploy-fresh.log" --fresh || exit 1
fi
timed_deploy "re-deploy" "$LOGS/deploy-again.log" || exit 1
check "re-deploy changes nothing" grep -q 'changed=0 ' "$LOGS/deploy-again.log"

# --- Phase (c): maintenance -----------------------------------------------------------
vm 'systemctl stop hc-listener 2>/dev/null; rm -f /tmp/hc.log; systemd-run --unit=hc-listener python3 -c "
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get(\"Content-Length\", 0)))
        open(\"/tmp/hc.log\", \"a\").write(self.path + \"\n\"); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer((\"127.0.0.1\", 8098), H).serve_forever()" >/dev/null; sleep 1'
check "maintenance run succeeds (OS + containers)" vm 'vpn-maintenance --no-reboot'
check "healthchecks start + success pings" vm 'grep -qx /hc/start /tmp/hc.log && grep -qx /hc /tmp/hc.log'
check "timers scheduled, apt's own upgrade timer off" vm 'systemctl is-active -q vpn-maintenance.timer vpn-heal.timer && ! systemctl is-enabled -q apt-daily-upgrade.timer'

# A bad container update is rolled back and quarantined (local registry stands in for Docker Hub).
vm 'docker rm -f e2e-registry >/dev/null 2>&1; docker run -d --name e2e-registry --network host registry:2 >/dev/null &&
    sleep 2 && docker tag "$(jq -r .pinned.caddy.image /opt/vpn/images.json)" localhost:5000/caddy:2 &&
    docker push -q localhost:5000/caddy:2 >/dev/null &&
    jq ".services.caddy.track = \"localhost:5000/caddy:2\"" /etc/vpn/maintenance.json > /tmp/m.json &&
    cp /tmp/m.json /etc/vpn/maintenance.json' >>"$LOGS/checks.log" 2>&1
check "container update to a new digest" vm 'rm -f /tmp/hc.log; vpn-maintenance --skip-os --no-reboot | grep -q "updated: caddy"'
vm 'mkdir -p /tmp/broken && printf "FROM localhost:5000/caddy:2\nCMD [\"sleep\",\"infinity\"]\n" > /tmp/broken/Dockerfile &&
    docker build -q -t localhost:5000/caddy:2 /tmp/broken >/dev/null && docker push -q localhost:5000/caddy:2 >/dev/null' \
    >>"$LOGS/checks.log" 2>&1
check "unhealthy update is rolled back" vm 'rm -f /tmp/hc.log; vpn-maintenance --skip-os --no-reboot | grep -q "ROLLED BACK: caddy"'
check "rollback sends a /fail ping" vm 'grep -qx /hc/fail /tmp/hc.log'
check "caddy healthy again after rollback" vm 'docker inspect -f "{{.State.Health.Status}}" caddy | grep -qx healthy'
check "quarantined digest is skipped next time" vm 'vpn-maintenance --skip-os --no-reboot | grep -q "skipped: caddy"'
timed_deploy "re-deploy restores the normal caddy track" "$LOGS/deploy-restore.log" || true
check "caddy pinned to Docker Hub again" vm 'jq -e ".pinned.caddy.track == \"caddy:2\"" /opt/vpn/images.json'
vm 'docker rm -f e2e-registry >/dev/null 2>&1' || true

check "vpn-heal starts a stopped container" vm 'docker stop adguard >/dev/null && vpn-heal && sleep 3 && docker ps --format "{{.Names}}" | grep -qx adguard'

# Reboot when an update requires it.
vm 'touch /run/reboot-required; systemd-run --unit=e2e-maint vpn-maintenance --skip-os --skip-containers >/dev/null' || true
sleep 20
for _ in $(seq 1 30); do vm 'test ! -e /run/reboot-required && systemctl is-active -q vpn-stack' >/dev/null 2>&1 && break; sleep 5; done
check "maintenance rebooted the server when required" vm 'grep -q "\"reboot\": \"now\"" /var/lib/vpn/maintenance.json && test "$(cut -d. -f1 /proc/uptime)" -lt 300'
sleep 45
check "everything healthy after the reboot" vm 'vpn-status | grep -q "✘" && exit 1 || exit 0'

# --- VPN client checks ------------------------------------------------------------------
WGPW=$(secret wg_easy_password)
SERVER2=$(vm "ip -4 -o addr | awk '/192\\.168\\.104\\./ {print \$4}' | cut -d/ -f1")
vm "vpn-breakglass wg-easy >/dev/null; A='admin:$WGPW'
    [ \"\$(curl -s -u \"\$A\" http://127.0.0.1:51821/api/client | jq length)\" -gt 0 ] ||
      curl -s -u \"\$A\" -H 'Content-Type: application/json' -X POST http://127.0.0.1:51821/api/client -d '{\"name\":\"e2e\",\"expiresAt\":null}' >/dev/null
    ID=\$(curl -s -u \"\$A\" http://127.0.0.1:51821/api/client | jq '.[0].id')
    curl -s -u \"\$A\" http://127.0.0.1:51821/api/client/\$ID/configuration > /tmp/e2e-client.conf
    vpn-breakglass off >/dev/null" >>"$LOGS/checks.log" 2>&1
vm 'cat /tmp/e2e-client.conf; rm -f /tmp/e2e-client.conf' | sed "s/^Endpoint = .*/Endpoint = $SERVER2:51820/" |
  limactl shell --workdir / vpn-client -- sudo tee /tmp/wg0.conf >/dev/null
limactl shell --workdir / vpn-client -- sudo tee /tmp/client-checks.sh <"$HERE/client-checks.sh" >/dev/null
limactl shell --workdir / vpn-client -- sudo bash -c "chmod +x /tmp/client-checks.sh; /tmp/client-checks.sh $SERVER2 $D >/dev/null 2>&1; grep -E '^(PASS|FAIL)' /tmp/client-results.txt" \
  >"$LOGS/client.log" 2>&1
while read -r status name; do result "$status" "client: $name"; done <"$LOGS/client.log"

# --- Phase (d): rebuild the server; existing devices must keep working -----------------
# shellcheck disable=SC2329  # called through check()
client_handshake() {
  limactl shell --workdir / vpn-client -- sudo bash -c "sed -i 's/^Endpoint = .*/Endpoint = $1:51820/' /tmp/wg0.conf
    cp /tmp/wg0.conf /etc/wireguard/wg0.conf; sed -i '/^DNS = /d' /etc/wireguard/wg0.conf
    wg-quick up wg0 >/dev/null 2>&1; sleep 4; ok=\$(wg show wg0 latest-handshakes | awk '{print \$2}')
    curl -s -m 8 -o /dev/null https://deb.debian.org/; rc=\$?; wg-quick down wg0 >/dev/null 2>&1
    [ \"\$ok\" -gt 0 ] && [ \$rc -eq 0 ]"
}
server_key() { vm 'wg show wg0 public-key'; }
if [[ $REBUILD_TESTS == 1 ]]; then
  check "identity saved in the vault" bash -c "cd '$REPO' && uv run --quiet python -m wizard --show-secret wg_easy_dump >/dev/null"
  KEY_BEFORE=$(server_key)
  timed_deploy "rebuild (identity from the vault)" "$LOGS/deploy-rebuild1.log" --rebuild || true
  check "rebuild restored the identity" grep -q "restoring its identity" "$LOGS/deploy-rebuild1.log"
  check "same WireGuard server key after rebuild" test "$(server_key)" = "$KEY_BEFORE"
  SERVER2=$(vm "ip -4 -o addr | awk '/192\\.168\\.104\\./ {print \$4}' | cut -d/ -f1")
  check "an existing device reconnects unchanged" client_handshake "$SERVER2"

  # Full encrypted backups: back up, rebuild, restore.
  (cd "$REPO" && uv run --quiet python tests/vm/set_config.py settings.full_backups=true) >>"$LOGS/checks.log"
  timed_deploy "deploy with backups on" "$LOGS/deploy-backup.log" || true
  check "encrypted backup written" bash -c "ls '$REPO'/backups/vpntest.example.test-*.tar.gz.age"
  check "backup is age-encrypted" bash -c "head -c 21 \"\$(ls -t '$REPO'/backups/vpntest.example.test-*.tar.gz.age | head -1)\" | grep -q age-encryption.org"
  vm 'sqlite3 /opt/vpn/authelia/config/db.sqlite3 "insert into user_preferences (username, second_factor_method) values (\"e2e-marker\", \"totp\")"' >>"$LOGS/checks.log" 2>&1
  (cd "$REPO" && ./backup.sh) >>"$LOGS/checks.log" 2>&1
  timed_deploy "rebuild (restore the backup)" "$LOGS/deploy-rebuild2.log" --rebuild || true
  check "rebuild restored the backup" grep -q "found the backup" "$LOGS/deploy-rebuild2.log"
  check "Authelia data came back from the backup" vm 'sqlite3 /opt/vpn/authelia/config/db.sqlite3 "select 1 from user_preferences where username=\"e2e-marker\"" | grep -q 1'
  check "same WireGuard server key after restore" test "$(server_key)" = "$KEY_BEFORE"
  SERVER2=$(vm "ip -4 -o addr | awk '/192\\.168\\.104\\./ {print \$4}' | cut -d/ -f1")
  check "an existing device reconnects after restore" client_handshake "$SERVER2"
  (cd "$REPO" && uv run --quiet python tests/vm/set_config.py settings.full_backups=false) >>"$LOGS/checks.log"
fi

echo
echo "== $(printf '%s\n' "${RESULTS[@]}" | grep -c '^PASS') passed, $(printf '%s\n' "${RESULTS[@]}" | grep -c '^FAIL') failed in $(( $(date +%s) - START ))s"
exit $FAILED
