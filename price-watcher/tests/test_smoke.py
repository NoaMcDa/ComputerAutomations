def test_package_imports():
    import price_watcher
    from price_watcher import cli, fetcher, state, watchlist  # noqa: F401

    assert price_watcher.__version__
