from price_watcher import paths


def test_app_dirs_are_isolated(_isolated_app_dirs):
    """Per-OS data/log dirs must resolve inside the test's temp folder."""
    tmp_root = _isolated_app_dirs.parent
    assert paths.data_dir().is_relative_to(tmp_root)
    assert paths.log_dir().is_relative_to(tmp_root)
