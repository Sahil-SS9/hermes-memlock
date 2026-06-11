"""Tests for MemLock: import styles and filesystem isolation."""
import importlib.util
import os
import sys
from pathlib import Path

import store as store_mod

_PDIR = Path(__file__).resolve().parent.parent


def test_store_dir_resolved_at_call_time(monkeypatch, tmp_path):
    """HERMES_HOME changed after import is honoured."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert store_mod._store_dir() == first / "memlock"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert store_mod._store_dir() == second / "memlock"


def test_store_writes_under_tmp_only(isolated_hermes_home, tmp_path):
    """A saved store lands under the isolated home, nowhere else."""
    s = store_mod.SessionStore("iso-check")
    s.add_anchor("a1", "text", "t", priority=50, probes=["text"], pinned=True)
    expected = isolated_hermes_home / "memlock" / "iso-check.json"
    assert expected.exists()
    real = Path(os.path.expanduser("~/.hermes/memlock/iso-check.json"))
    assert not real.exists()


def test_import_works_as_plain_module():
    """__init__.py loads without a package context (absolute-import fallback)."""
    spec = importlib.util.spec_from_file_location(
        "memlock_plain_check", _PDIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "register")


def test_import_works_as_package():
    """__init__.py loads as a package (relative imports)."""
    name = "memlock_pkg_check"
    spec = importlib.util.spec_from_file_location(
        name, _PDIR / "__init__.py", submodule_search_locations=[str(_PDIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        assert hasattr(mod, "register")
    finally:
        sys.modules.pop(name, None)
