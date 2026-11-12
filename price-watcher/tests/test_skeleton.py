"""Issue #2: project skeleton and configuration."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from price_watcher import paths

ROOT = Path(__file__).resolve().parents[1]


def test_module_help_runs():
    out = subprocess.run(
        [sys.executable, "-m", "price_watcher", "--help"],
        capture_output=True, text=True, cwd=ROOT, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "check" in out.stdout


def test_env_example_has_no_real_addresses_or_secrets():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert emails and all(e.endswith("@example.com") for e in emails)
    assert re.search(r"PW_SMTP_PASSWORD=xxxx", text)


def test_gitignore_covers_secrets_and_venv():
    gi = (ROOT.parent / ".gitignore").read_text(encoding="utf-8")
    for entry in (".env", ".venv/", "__pycache__/"):
        assert entry in gi


def test_per_user_dirs_are_created_and_utf8_safe(_isolated_app_dirs):
    d = paths.data_dir()
    assert d.is_dir()
    f = d / "בדיקה ₪.txt"
    f.write_text("מחיר ₪", encoding="utf-8")
    assert f.read_text(encoding="utf-8") == "מחיר ₪"
    assert paths.log_dir().is_dir()


def test_setup_scripts_exist():
    assert (ROOT / "setup.sh").is_file()
    assert (ROOT / "setup.ps1").is_file()


def test_single_user_agent_source():
    """#40: one shared User-Agent string, no per-module Chrome versions."""
    src = ROOT / "src" / "price_watcher"
    offenders = [p.name for p in src.rglob("*.py")
                 if "Chrome/" in p.read_text(encoding="utf-8") and p.name != "http_headers.py"]
    assert offenders == []
