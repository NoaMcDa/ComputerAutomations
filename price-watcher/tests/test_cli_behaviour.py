import pytest
from typer.testing import CliRunner

from price_watcher import cli, logging_setup

runner = CliRunner()


@pytest.fixture
def levels(monkeypatch):
    """Record levels passed to the real validator, without installing handlers."""
    seen = []
    real = logging_setup.configure

    def fake(level="INFO"):
        if level.upper() not in logging_setup.VALID_LEVELS:
            return real(level)  # raises ValueError
        seen.append(level)

    monkeypatch.setattr(cli, "configure_logging", fake)
    return seen


def test_check_exit_code_propagates(monkeypatch, levels):
    import price_watcher.scheduler as s
    monkeypatch.setattr(s, "run_check", lambda dry_run=False: 1)
    assert runner.invoke(cli.app, ["check"]).exit_code == 1
    monkeypatch.setattr(s, "run_check", lambda dry_run=False: 0)
    assert runner.invoke(cli.app, ["check"]).exit_code == 0


def test_digest_exit_code_propagates(monkeypatch, levels):
    import price_watcher.digest as d
    monkeypatch.setattr(d, "run_digest", lambda dry_run=False: 1)
    assert runner.invoke(cli.app, ["digest"]).exit_code == 1


def test_pw_log_level_env_is_default(monkeypatch, levels):
    monkeypatch.setenv("PW_LOG_LEVEL", "DEBUG")
    assert runner.invoke(cli.app, ["version"]).exit_code == 0
    assert levels == ["DEBUG"]
    assert runner.invoke(cli.app, ["--log-level", "ERROR", "version"]).exit_code == 0
    assert levels == ["DEBUG", "ERROR"]


def test_invalid_log_level_is_one_line_error(levels):
    r = runner.invoke(cli.app, ["--log-level", "LOUD", "version"])
    assert r.exit_code != 0
    assert isinstance(r.exception, SystemExit)
    assert "Invalid log level" in r.output
    assert "Traceback" not in r.output


def test_real_logging_configure_rejects_bad_level():
    with pytest.raises(ValueError):
        logging_setup.configure("LOUD")
