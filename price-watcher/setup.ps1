# Price Watcher setup for Windows. Installs uv (if missing), creates a venv,
# installs dependencies, and installs the Playwright Chromium browser.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found - installing uv..."
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "Creating virtual environment with Python 3.11+..."
uv venv --python 3.11

Write-Host "Installing project dependencies..."
uv pip install -e .

Write-Host "Installing Playwright Chromium (used for JS-heavy pages)..."
& ".venv\Scripts\python.exe" -m playwright install chromium

if (-not (Test-Path ".env")) {
    Write-Host "Creating .env from .env.example - remember to fill in your SMTP credentials."
    Copy-Item .env.example .env
}

Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Edit price-watcher\.env and set PW_EMAIL_TO + SMTP credentials."
Write-Host "  2. Activate the venv:  .\.venv\Scripts\Activate.ps1"
Write-Host "  3. Try:                python -m price_watcher --help"
