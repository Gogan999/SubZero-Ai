#!/usr/bin/env bash
# Runs SubZero AI on macOS or Linux.
#   scripts/run.sh --test-mode --open   sample sign-ins and a pretend Claude
#   scripts/run.sh check                check config.env, Slack, and the Claude API key
#   scripts/run.sh                      start the app (reads ./data/config.env or ./config.env)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up a private Python environment in .venv ..."
  "$PYTHON" -m venv .venv
fi

if command -v sha256sum >/dev/null 2>&1; then
  HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
else
  HASH="$(shasum -a 256 requirements.txt | cut -d' ' -f1)"
fi
if [ ! -f .venv/requirements.sha256 ] || [ "$(cat .venv/requirements.sha256)" != "$HASH" ]; then
  echo "Installing packages ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  echo "$HASH" > .venv/requirements.sha256
fi

exec .venv/bin/python -m subzero_ai "$@"
