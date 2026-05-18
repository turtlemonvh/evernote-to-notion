#!/usr/bin/env bash
# Run a command inside the evernote-to-notion conda env.
# Usage: ./scripts/conda-run.sh <command> [args...]
#
# Allows Claude Code to invoke env-installed tools (evernote-backup, python)
# without needing to source conda activation each time.

set -euo pipefail

ENV_NAME="evernote-to-notion"

CONDA_BASE="$(conda info --base 2>/dev/null || true)"
if [[ -z "${CONDA_BASE}" ]]; then
  echo "error: conda not found on PATH" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "${CONDA_BASE}/etc/profile.d/conda.sh"

if ! conda activate "${ENV_NAME}" 2>/dev/null; then
  echo "error: conda env '${ENV_NAME}' not found. Create it with:" >&2
  echo "  conda create -n ${ENV_NAME} python=3.11 -y" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <command> [args...]" >&2
  exit 2
fi

exec "$@"
