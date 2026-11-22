"""pytest plugin used once, offline, to map each test to the sqlglot lines it runs,
the fixture files it opens, and the dialects / expression classes it touches."""
import builtins, io, json, os, sys, importlib, pkgutil
import pytest

ROOT = os.path.abspath(os.getcwd())
OUT = os.environ["COVMAP_OUT"]
_cov = None
_ctx = ["import"]
_extra = {}  # ctx -> {"files": set, "dialects": set, "exprs": set}


def _rec(kind, val):
    d = _extra.setdefault(_ctx[0], {"files": set(), "dialects": set(), "exprs": set()})
    d[kind].add(val)


def _wrap_open(orig):
    def op(file, *a, **k):
        try:
            p = os.path.abspath(os.fspath(file))
            if p.startswith(ROOT):
                _rec("files", os.path.relpath(p, ROOT))
        except Exception:
            pass
        return orig(file, *a, **k)
    return op


def pytest_configure(config):
    global _cov
    if not hasattr(config, "workerinput") and config.option.numprocesses:
        return
    import coverage
    wid = getattr(config, "workerinput", {}).get("workerid", "main")
    _cov = coverage.Coverage(data_file=os.path.join(OUT, f"cov.{wid}"), include=[os.path.join(ROOT, "sqlglot", "*"), os.path.join(ROOT, "tests", "helpers.py"), os.path.join(ROOT, "tests", "__init__.py"), os.path.join(ROOT, "tests", "leakcheck.py")], branch=False)
    _cov.start()
    _cov.switch_context("import")
    builtins.open = _wrap_open(builtins.open)
    io.open = builtins.open
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if "sqlglotc" in m.name:
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:
            print("import fail", m.name, e, file=sys.stderr)
    from sqlglot.dialects.dialect import Dialect
    from sqlglot import expressions as exp
    orig_init = Dialect.__init__
    def dinit(self, *a, **k):
        _rec("dialects", type(self).__name__)
        return orig_init(self, *a, **k)
    Dialect.__init__ = dinit
    base = exp.Expr if hasattr(exp, "Expr") else exp.Expression
    orig_e = base.__init__
    def einit(self, *a, **k):
        _rec("exprs", type(self).__name__)
        return orig_e(self, *a, **k)
    base.__init__ = einit


def _serial_pool():
    import pickle
    from concurrent.futures import Future

    class SerialPool:
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
            if initializer:
                initializer(*initargs)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def shutdown(self, *a, **k):
            pass
        def submit(self, fn, *a, **k):
            f = Future()
            try:
                a, k = pickle.loads(pickle.dumps((a, k)))
                f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
            except BaseException as e:
                f.set_exception(e)
            return f
    return SerialPool


def pytest_collection_modifyitems(items):
    pool = _serial_pool()
    for name, m in list(sys.modules.items()):
        if name.startswith("tests") and hasattr(m, "ProcessPoolExecutor"):
            m.ProcessPoolExecutor = pool


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # test modules import (and often parse fixtures) while being collected
    if isinstance(collector, pytest.Module):
        _switch("M|" + collector.nodeid)
        yield
        _switch("import")
    else:
        yield


def _switch(name):
    _ctx[0] = name
    if _cov:
        _cov.switch_context(name)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _switch("S|" + item.nodeid)
    yield
    _switch("C|" + item.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _switch("T|" + item.nodeid)
    yield
    _switch("other")


def pytest_unconfigure(config):
    if _cov is None:
        return
    _cov.stop()
    _cov.save()
    wid = getattr(config, "workerinput", {}).get("workerid", "main")
    with open(os.path.join(OUT, f"extra.{wid}.json"), "w") as f:
        json.dump({k: {kk: sorted(vv) for kk, vv in v.items()} for k, v in _extra.items()}, f)
