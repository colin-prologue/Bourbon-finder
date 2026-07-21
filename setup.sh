#!/bin/bash
# One-time setup for nc-bourbon-finder on macOS.
# Usage:  ./setup.sh        (then see the final instructions it prints)
set -e
cd "$(dirname "$0")"

PY=python3
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  for cand in python3.13 python3.12 python3.11; do
    if command -v $cand >/dev/null; then PY=$cand; break; fi
  done
fi
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
  echo "Python 3.11+ not found. Install with:  brew install python@3.12"; exit 1
fi
echo "Using $($PY --version)"

[ -d .venv ] || $PY -m venv .venv
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "Dependencies installed."

python -m pytest tests/ -q && echo "Tests pass."

echo
echo "=========================================================="
echo "Setup complete. Two manual steps remain:"
echo
echo "1) Gmail App Password (needed once):"
echo "   myaccount.google.com -> Security -> 2-Step Verification"
echo "   -> App passwords -> create one for 'nc-bourbon-finder'"
echo
echo "2) Run your first poll:"
echo "   export NCBOURBON_SMTP_PASSWORD='xxxx xxxx xxxx xxxx'"
echo "   source .venv/bin/activate"
echo "   python -m ncbourbon poll-stocks     # first run = baseline burst"
echo "   python -m ncbourbon status"
echo
echo "config.toml is pre-filled with colin@prologuegames.com."
echo "If you'd rather send from a personal Gmail, edit smtp_user"
echo "and from_addr in config.toml."
echo "=========================================================="
