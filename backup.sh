#!/usr/bin/env bash
# Save the server's state to this machine: the identity (WireGuard key + devices) into your vault,
# plus an encrypted backup if full backups are on. Run it after adding or removing devices.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./deploy.sh --backup "$@"
