def test_imports():
    import importlib

    for m in ["app", "app.scheduler"]:
        importlib.import_module(m)
