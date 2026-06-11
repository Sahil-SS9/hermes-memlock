"""Shared fixtures for MemLock tests.

The plugin module is loaded once, here, under the name "memlock" (the repo
directory name is not a valid package name). Tests receive it via the
``memlock`` fixture instead of repeating the importlib boilerplate.

Every test runs with HERMES_HOME pointed at a per-test tmp_path and with the
plugin's module-level state reset, so no test can touch the real ~/.hermes
or leak state into another test.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_PDIR = Path(__file__).resolve().parent.parent
if str(_PDIR) not in sys.path:
    sys.path.insert(0, str(_PDIR))

_spec = importlib.util.spec_from_file_location(
    "memlock", _PDIR / "__init__.py", submodule_search_locations=[str(_PDIR)],
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["memlock"] = _mod
_spec.loader.exec_module(_mod)


class FakeCtx:
    def __init__(self, cfg, session_id="test-s"):
        self._cfg = cfg
        self.session_id = session_id
        self.hooks = {}
        self.tools = {}
        self.commands = {}

    @property
    def config(self):
        return self._cfg

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"handler": handler, "schema": schema}

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a per-test directory and reset module state."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _mod._stores.clear()
    _mod._session_turns.clear()
    _mod._current_session_id = ""
    _mod._cfg = {}
    yield home


@pytest.fixture
def memlock():
    return _mod


@pytest.fixture
def fake_ctx_cls():
    return FakeCtx


@pytest.fixture
def base_cfg():
    return {
        "max_slots": 8, "max_reminder_chars": 600,
        "hard_reinject_turns": 40,
        "alert_floor": 70, "alert_cooldown_s": 1800,
        "drift_threshold": 0.5,
        "detection": "keyword",
        "anchors": [],
    }


@pytest.fixture
def plugin(memlock, base_cfg):
    """Register the plugin against a FakeCtx and start a test session."""
    fc = FakeCtx({"memlock": base_cfg}, session_id="test-s")
    memlock.register(fc)
    memlock._on_start(session_id="test-s")
    return fc, base_cfg
