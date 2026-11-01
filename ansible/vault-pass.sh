#!/bin/sh
# Ansible vault password source. ./deploy.sh exports VPN_VAULT_PASSWORD (from your OS
# keychain) for the duration of the run; nothing is written to disk.
if [ -z "${VPN_VAULT_PASSWORD:-}" ]; then
  echo "VPN_VAULT_PASSWORD is not set; run playbooks through ./deploy.sh" >&2
  exit 1
fi
printf '%s\n' "$VPN_VAULT_PASSWORD"
