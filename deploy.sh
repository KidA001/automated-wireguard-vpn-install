#!/usr/bin/env bash
# Deploy or update the WireGuard VPN server. See README.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v uv >/dev/null 2>&1; then
  echo "This script needs uv (https://docs.astral.sh/uv/) to manage its Python environment."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "Install uv with Homebrew now? [Y/n] " answer
    [[ "${answer:-y}" =~ ^[Yy]$ ]] || exit 1
    brew install uv
  else
    read -r -p "Install uv with the official installer (astral.sh)? [Y/n] " answer
    [[ "${answer:-y}" =~ ^[Yy]$ ]] || exit 1
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi

for tool in ssh ssh-keygen ssh-keyscan; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool" >&2; exit 1; }
done

uv sync --quiet --frozen 2>/dev/null || uv sync --quiet
exec uv run --quiet python -m wizard "$@"
