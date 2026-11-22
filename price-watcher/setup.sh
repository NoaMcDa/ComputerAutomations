#!/usr/bin/env bash
# Price Watcher setup for macOS. Installs uv (if missing), creates a venv,
# installs dependencies, and installs the Playwright Chromium browser.
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found — installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "Creating virtual environment with Python 3.11+..."
uv venv --python 3.11

echo "Installing project dependencies..."
uv pip install -e .

echo "Installing Playwright Chromium (used for JS-heavy pages)..."
.venv/bin/python -m playwright install chromium

if [ ! -f .env ]; then
    echo "Creating .env from .env.example — remember to fill in your SMTP credentials."
    cp .env.example .env
fi

echo
echo "Done. Next steps:"
echo "  1. Edit price-watcher/.env and set PW_EMAIL_TO + SMTP credentials."
echo "  2. Activate the venv:  source .venv/bin/activate"
echo "  3. Try:                python -m price_watcher --help"
