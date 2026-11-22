from datetime import date, time

import pytest

from settings import ConfigError, load_config, load_dotenv, DEFAULT_CONFIG_PATH


def test_shipped_config_is_valid_and_bootstrapped_for_afrit():
    cfg = load_config(DEFAULT_CONFIG_PATH)
    assert cfg.restaurant_name == "Afrit"
    assert (cfg.start, cfg.end) == (date(2026, 11, 12), date(2026, 12, 30))
    assert cfg.days_filter == ("weekend",)
    assert cfg.weekend_days == ("friday", "saturday")
    assert cfg.party_size == 2
    assert cfg.mode == "notify_only"
    assert cfg.email.frequency == "daily" and cfg.email.suppress_empty is True


def test_weekend_dates_full_range(config):
    dates = config.target_dates(today=date(2026, 11, 1))
    assert all(d.weekday() in (4, 5) for d in dates)
    # Range starts on Thursday 12.11 (excluded) and ends Wednesday 30.12 (excluded).
    assert dates[0] == date(2026, 11, 13)
    assert dates[-1] == date(2026, 12, 26)
    assert len(dates) == 14  # 7 Fridays + 7 Saturdays


def test_range_edges_are_inclusive(make_config):
    cfg = make_config(date_range={"start": "2026-11-13", "end": "2026-11-14"})
    assert cfg.target_dates(date(2026, 11, 1)) == [date(2026, 11, 13), date(2026, 11, 14)]
    cfg = make_config(date_range={"start": "2026-11-14", "end": "2026-11-14"})
    assert cfg.target_dates(date(2026, 11, 1)) == [date(2026, 11, 14)]


def test_past_dates_are_skipped(config):
    dates = config.target_dates(today=date(2026, 12, 19))
    assert dates == [date(2026, 12, 19), date(2026, 12, 25), date(2026, 12, 26)]
    assert config.target_dates(today=date(2026, 12, 31)) == []


def test_configurable_weekend_includes_thursday(make_config):
    cfg = make_config(weekend_days=["thursday", "friday", "saturday"])
    dates = cfg.target_dates(today=date(2026, 11, 12))
    assert dates[:3] == [date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 14)]
    # Thursday uses the evening override from config.json
    assert cfg.window_for(date(2026, 11, 12)).start == time(20, 0)
    assert cfg.window_for(date(2026, 11, 13)).start == time(19, 0)


def test_explicit_days_filter(make_config):
    cfg = make_config(days_filter=["sunday"])
    assert {d.weekday() for d in cfg.target_dates(date(2026, 11, 1))} == {6}


@pytest.mark.parametrize("override", [
    {"mode": "yolo"},
    {"party_size": 0},
    {"party_size": "2"},
    {"days_filter": ["funday"]},
    {"date_range": {"start": "2026-12-30", "end": "2026-11-12"}},
    {"time_window": {"start": "23:00", "end": "19:00"}},
    {"email_notifications": {"frequency": "hourly"}},
])
def test_invalid_config_rejected(make_config, override):
    with pytest.raises(ConfigError):
        make_config(**override)


def test_dotenv_does_not_override_real_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# c\nRO_SMTP_USER=file@example.com\nexport RO_EMAIL_TO='a@x.com'\nJUNK\n", encoding="utf-8")
    environ = {"RO_SMTP_USER": "real@example.com"}
    load_dotenv(env_file, environ)
    assert environ == {"RO_SMTP_USER": "real@example.com", "RO_EMAIL_TO": "a@x.com"}
