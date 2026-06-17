"""Tests for MemLock: durable pin persistence (memory-provider agnostic)."""
import json
from pathlib import Path

import persistence


def test_file_store_save_and_load(tmp_path):
    """FileStore saves a pin and loads it back."""
    store = persistence.FileStore(directory=tmp_path / "persist")
    anchor = {
        "id": "pin_test_1",
        "text": "Always use British English",
        "reminder": "British English",
        "priority": 80,
        "probes": ["British", "English"],
        "scope": "global",
        "pinned_at": 1234567890.0,
    }
    store.save_pin(anchor)

    loaded = store.load_pins()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "pin_test_1"
    assert loaded[0]["text"] == "Always use British English"
    assert loaded[0]["scope"] == "global"


def test_file_store_remove(tmp_path):
    """FileStore removes a pin."""
    store = persistence.FileStore(directory=tmp_path / "persist")
    store.save_pin({"id": "pin_rm", "text": "test", "scope": "global"})
    assert len(store.load_pins()) == 1

    store.remove_pin("pin_rm")
    assert len(store.load_pins()) == 0


def test_file_store_load_empty(tmp_path):
    """FileStore returns empty list when no pins exist."""
    store = persistence.FileStore(directory=tmp_path / "nonexistent")
    assert store.load_pins() == []


def test_file_store_corrupt_file_skipped(tmp_path):
    """Corrupt JSON files are skipped, not fatal."""
    store = persistence.FileStore(directory=tmp_path / "persist")
    store.save_pin({"id": "good", "text": "valid", "scope": "global"})
    # Write a corrupt file
    (tmp_path / "persist" / "bad.json").write_text("not json {{{")
    loaded = store.load_pins()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "good"


def test_file_store_missing_fields_filtered(tmp_path):
    """Pins missing id or text are filtered out."""
    store = persistence.FileStore(directory=tmp_path / "persist")
    store.save_pin({"id": "good", "text": "valid", "scope": "global"})
    # Manually write a pin with no id
    bad_path = tmp_path / "persist" / "no_id.json"
    bad_path.write_text(json.dumps({"text": "no id here", "scope": "global"}))
    loaded = store.load_pins()
    assert len(loaded) == 1
    assert loaded[0]["id"] == "good"


def test_get_store_file_default():
    """get_store('file') returns a FileStore."""
    store = persistence.get_store("file")
    assert isinstance(store, persistence.FileStore)


def test_get_store_unknown_falls_back():
    """Unknown backend falls back to FileStore with warning."""
    store = persistence.get_store("nonexistent_backend_xyz")
    assert isinstance(store, persistence.FileStore)


def test_get_store_mnemosyne_falls_back():
    """Mnemosyne backend (deferred) falls back to FileStore."""
    store = persistence.get_store("mnemosyne")
    assert isinstance(store, persistence.FileStore)


def test_persist_dir_honours_hermes_home(monkeypatch, tmp_path):
    """_persist_dir() resolves HERMES_HOME at call time."""
    first = tmp_path / "first"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert persistence._persist_dir() == first / "memlock" / "persist"

    second = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert persistence._persist_dir() == second / "memlock" / "persist"
