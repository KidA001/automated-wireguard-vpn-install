#!/usr/bin/env bash
# Take an encrypted backup of the server's app state now (needs full backups turned on).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec ./deploy.sh --backup "$@"
