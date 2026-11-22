import pytest
from typer.testing import CliRunner

from price_watcher import cli, config, logging_setup

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_handlers(monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda level="INFO": None)


def test_non_numeric_env_raises_config_error(monkeypatch):
    monkeypatch.setenv("PW_SMTP_PORT", "abc")
    with pytest.raises(config.ConfigError, match="PW_SMTP_PORT"):
        config.load()
    monkeypatch.setenv("PW_SMTP_PORT", "587")
    monkeypatch.setenv("PW_DELAY_MIN_SEC", "fast")
    with pytest.raises(config.ConfigError, match="PW_DELAY_MIN_SEC"):
        config.load()


def test_missing_required_env_raises_config_error(monkeypatch):
    monkeypatch.delenv("PW_SMTP_USER")
    with pytest.raises(config.ConfigError, match="PW_SMTP_USER"):
        config.load()
    assert config.load(require_email=False).email.smtp_user == ""


def test_cli_reports_config_error_in_one_line(monkeypatch):
    monkeypatch.setenv("PW_FAILURE_THRESHOLD", "x")
    r = runner.invoke(cli.app, ["check"])
    assert r.exit_code != 0
    assert "PW_FAILURE_THRESHOLD" in r.output
    assert "Traceback" not in r.output
    assert isinstance(r.exception, SystemExit)


def test_check_dry_run_does_not_need_smtp(monkeypatch):
    import price_watcher.watchlist as w
    for v in ("PW_SMTP_USER", "PW_SMTP_PASSWORD", "PW_EMAIL_FROM", "PW_EMAIL_TO"):
        monkeypatch.delenv(v)
    monkeypatch.setattr(w, "load_all", lambda: [])
    assert runner.invoke(cli.app, ["check", "--dry-run"]).exit_code == 0
    r = runner.invoke(cli.app, ["check"])
    assert r.exit_code != 0 and "PW_SMTP_USER" in r.output
