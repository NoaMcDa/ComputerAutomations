from price_watcher import paths


def test_app_dirs_are_isolated(_isolated_app_dirs):
    assert str(paths.data_dir()).startswith(str(_isolated_app_dirs))
