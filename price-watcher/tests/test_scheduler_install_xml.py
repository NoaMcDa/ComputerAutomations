import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from price_watcher import scheduler

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture
def run_log(monkeypatch):
    log = []

    def fake(cmd, **kw):
        log.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(scheduler, "_repo_dir", lambda: Path("C:/R&D <x>/price-watcher"))
    monkeypatch.setattr(scheduler, "_venv_python", lambda: "C:/Py & Co/python.exe")
    return log


@pytest.mark.parametrize("fn,xml", [
    ("_install_schtasks", "task-scheduler.xml"),
    ("_install_digest_schtasks", "task-scheduler-digest.xml"),
])
def test_task_xml_wellformed_escaped_with_battery(run_log, fn, xml):
    getattr(scheduler, fn)()
    text = (scheduler.data_dir() / xml).read_text(encoding="utf-16")
    root = ET.fromstring(text.replace('encoding="UTF-16"', ""))
    settings = root.find("t:Settings", NS)
    tags = [c.tag.split("}")[1] for c in settings]
    assert settings.find("t:DisallowStartIfOnBatteries", NS).text == "false"
    assert settings.find("t:StopIfGoingOnBatteries", NS).text == "false"
    assert (tags.index("DisallowStartIfOnBatteries") < tags.index("StopIfGoingOnBatteries")
            < tags.index("StartWhenAvailable"))
    ex = root.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text == "C:/Py & Co/python.exe"
    assert ex.find("t:WorkingDirectory", NS).text == str(Path("C:/R&D <x>/price-watcher"))


def test_launchd_digest_label_not_counted_as_check(monkeypatch):
    out = f"123\t0\t{scheduler._DIGEST_PLIST_LABEL}\n-\t0\tcom.other\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out, stderr=""))
    assert scheduler._launchd_installed() is False
    assert scheduler._launchd_installed(scheduler._DIGEST_PLIST_LABEL) is True


def test_status_reports_digest_schedule(monkeypatch, capsys):
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: SimpleNamespace(
        returncode=0 if scheduler._DIGEST_WIN_TASK_NAME in cmd else 1, stdout="", stderr=""))
    scheduler.status()
    out = capsys.readouterr().out
    assert "Digest schedule" in out and "installed" in out
