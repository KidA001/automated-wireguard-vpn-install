#!/bin/bash
# Runs inside the client VM (tests/vm/client.yaml). Brings the tunnel up, checks
# everything that matters from a device's point of view, then tears it down.
# Usage: client-checks.sh <server-ip>   (expects /tmp/wg0.conf)
set -u
SERVER="$1"
OUT=/tmp/client-results.txt
: >"$OUT"
pass() { echo "PASS  $*" >>"$OUT"; }
fail() { echo "FAIL  $*" >>"$OUT"; }
check() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then pass "$name"; else fail "$name"; fi; }
check_not() { local name="$1"; shift; if "$@" >/dev/null 2>&1; then fail "$name"; else pass "$name"; fi; }
status() { dig +time=3 +tries=1 "$@" | grep -o 'status: [A-Z]*' | cut -d' ' -f2; }

# Use the tunnel's DNS settings without needing resolvconf.
sed '/^DNS = /d' /tmp/wg0.conf >/etc/wireguard/wg0.conf
chmod 600 /etc/wireguard/wg0.conf
wg-quick up wg0 >>"$OUT" 2>&1 || { fail "wg-quick up"; exit 1; }
sleep 3

check "handshake with server" bash -c "[ \$(wg show wg0 latest-handshakes | awk '{print \$2}') -gt 0 ]"
check "IPv4 internet routes via tunnel" bash -c "ip route get 1.1.1.1 | grep -q 'dev wg0'"
check "HTTPS through tunnel" curl -s -m 10 -o /dev/null https://deb.debian.org/
check "DNS via tunnel resolver" bash -c "[ \"$(status @10.8.0.1 debian.org)\" = NOERROR ]"
check "ads blocked" bash -c "[ \"$(status @10.8.0.1 doubleclick.net)\" = NXDOMAIN ]"
check "forced DNS: 8.8.8.8 is intercepted (ad blocked)" bash -c "[ \"$(status @8.8.8.8 doubleclick.net)\" = NXDOMAIN ]"
check "forced DNS: 8.8.8.8 still resolves normal names" bash -c "[ \"$(status @8.8.8.8 debian.org)\" = NOERROR ]"
check_not "DoT bypass to 1.1.1.1:853 blocked" nc -z -w 4 1.1.1.1 853
check_not "AdGuard UI not exposed on tunnel" curl -s -m 4 http://10.8.0.1:3000/
check_not "wg-easy UI not exposed on tunnel" curl -s -m 4 http://10.8.0.1:51821/
check "IPv6 routed into tunnel (no leak)" bash -c "ip -6 route get 2001:4860:4860::8888 | grep -q 'dev wg0'"
check_not "IPv6 internet refused (server has no public IPv6)" curl -6 -s -m 6 -o /dev/null https://ipv6.google.com/
check "SSH to server still reachable through tunnel" nc -z -w 4 "$SERVER" 22

{
  wg show wg0 transfer
  wg-quick down wg0 2>&1
  echo DONE
} >>"$OUT"
