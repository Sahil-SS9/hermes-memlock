"""Pin-file integrity manifest tests (Stage 4 — ContextNest).

Covers the durable-store manifest (written atomically on every change,
enforced on load with quarantine) and the session store's advisory
``self_sha256``. All failure paths are fail-open: quarantine + warning,
bootstrap baseline, never an exception escaping to the caller.
"""
from __future__ import annotations

import json
import logging

import memlock_core.persistence as persistence
from memlock_core.store import SessionStore


def _make_anchor(pin_id: str, text: str) -> dict:
    return {
        "id": pin_id,
        "text": text,
        "reminder": text[:120],
        "priority": 50,
        "probes": [text.split()[0]],
        "scope": "global",
    }


# ── durable manifest basics ──────────────────────────────────────────────


def test_save_pin_writes_manifest(tmp_path):
    """save_pin produces persist/manifest.json covering the pin file."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_m1", "manifested rule one"))
    store.save_pin(_make_anchor("pin_m2", "manifested rule two"))

    mpath = pdir / "manifest.json"
    assert mpath.exists()
    manifest = json.loads(mpath.read_text())
    assert manifest["version"] == 1
    assert "generated_at" in manifest
    assert set(manifest["pins"]) == {"pin_m1", "pin_m2"}
    entry = manifest["pins"]["pin_m1"]
    assert len(entry["sha256"]) == 64
    assert entry["size"] == (pdir / "pin_m1.json").stat().st_size


def test_manifest_rewritten_on_every_change(tmp_path):
    """save_pin / remove_pin / rollback each refresh the manifest."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_a", "first version"))
    first = json.loads((pdir / "manifest.json").read_text())

    # upsert changes file bytes -> manifest hash must move
    store.save_pin(_make_anchor("pin_a", "second version"))
    second = json.loads((pdir / "manifest.json").read_text())
    assert second["generated_at"] >= first["generated_at"]
    assert (
        second["pins"]["pin_a"]["sha256"] != first["pins"]["pin_a"]["sha256"]
    )

    # remove drops the entry entirely (and does not list manifest itself)
    store.remove_pin("pin_a")
    third = json.loads((pdir / "manifest.json").read_text())
    assert "pin_a" not in third["pins"]
    assert set(third["pins"]) == set()


def test_tampered_pin_file_is_quarantined(tmp_path, caplog):
    """A pin file edited after the manifest write is skipped + logged."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_good", "untampered rule"))
    store.save_pin(_make_anchor("pin_evil", "original evil rule"))

    # tamper AFTER the manifest write
    evil = pdir / "pin_evil.json"
    data = json.loads(evil.read_text())
    data["text"] = "tampered injection payload"
    evil.write_text(json.dumps(data, indent=2))

    with caplog.at_level(logging.WARNING, logger="memlock_core.persistence"):
        loaded = store.load_pins()

    assert [p["id"] for p in loaded] == ["pin_good"], (
        "tampered file must be quarantined; others still load"
    )
    assert any(
        "quarantine" in rec.message and "pin_evil.json" in rec.message
        for rec in caplog.records
    ), caplog.records


def test_missing_manifest_bootstraps_baseline(tmp_path):
    """No manifest.json => compute a fresh baseline from current files."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_b1", "bootstrap one"))
    store.save_pin(_make_anchor("pin_b2", "bootstrap two"))
    (pdir / "manifest.json").unlink()  # simulate pre-manifest install

    loaded = persistence.FileStore(directory=pdir).load_pins()
    assert sorted(p["id"] for p in loaded) == ["pin_b1", "pin_b2"]
    # bootstrap is non-destructive: no new manifest forced onto disk here;
    # the next save/remove rewrites it.


def test_corrupt_manifest_fails_open_with_baseline_rebuild(tmp_path, caplog):
    """Unparseable manifest => warning + treat files as trustworthy."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_c1", "survives corrupt manifest"))
    (pdir / "manifest.json").write_text("{not valid json !!!")

    with caplog.at_level(logging.WARNING, logger="memlock_core.persistence"):
        loaded = store.load_pins()
    assert [p["id"] for p in loaded] == ["pin_c1"]
    assert any("rebuilding baseline" in rec.message for rec in caplog.records)


def test_verify_false_skips_integrity_check(tmp_path):
    """verify=False loads tampered files (opt-out for admin tooling)."""
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)
    store.save_pin(_make_anchor("pin_t", "before tamper"))
    evil = pdir / "pin_t.json"
    data = json.loads(evil.read_text())
    data["text"] = "after tamper"
    evil.write_text(json.dumps(data, indent=2))

    unchecked = persistence.FileStore(directory=pdir, verify=False)
    assert [p["id"] for p in unchecked.load_pins()] == ["pin_t"]


def test_global_pin_rollback_repersists_and_updates_manifest(
    monkeypatch, tmp_path,
):
    """End-to-end: service-level global rollback keeps manifest in sync."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from memlock_core import MemlockService

    svc = MemlockService({"max_pins": 16})
    svc.on_start(session_id="int-s")
    out = svc.pin_handler(
        {"text": "Global integrity rule about falcons", "scope": "global"},
        session_id="int-s",
    )
    assert "Pinned instruction" in out
    store = svc.get_store("int-s")
    assert store is not None
    pin_id = next(iter(store.anchors()))
    svc.update_pin(store, pin_id, {"text": "Edited integrity rule about owls"})

    svc.rollback_pin(store, pin_id)

    pdir = home / "memlock" / "persist"
    rolled = json.loads((pdir / f"{pin_id}.json").read_text())
    assert rolled["text"] == "Global integrity rule about falcons"
    manifest = json.loads((pdir / "manifest.json").read_text())
    import hashlib

    actual = hashlib.sha256((pdir / f"{pin_id}.json").read_bytes()).hexdigest()
    assert manifest["pins"][pin_id]["sha256"] == actual


# ── session store self_sha256 (advisory) ─────────────────────────────────


def _raw_store_file(store: SessionStore) -> dict:
    return json.loads(store._path.read_text())


def test_session_store_writes_self_sha256(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    store = SessionStore("int-ss")
    store.add_anchor(
        "pin_ss", "self hash rule", "self hash", 50, ["hash"],
    )
    raw = _raw_store_file(store)
    assert "self_sha256" in raw
    import hashlib

    expected = hashlib.sha256(
        json.dumps(raw["anchors"], sort_keys=True, ensure_ascii=False).encode(),
    ).hexdigest()
    assert raw["self_sha256"] == expected


def test_session_store_self_sha256_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    store = SessionStore("rt-s")
    store.add_anchor("pin_rt", "roundtrip rule", "roundtrip", 50, ["rt"])
    store.record_compaction("abc123", 3)

    reloaded = SessionStore("rt-s")  # fresh instance reads from disk
    anchor = reloaded.get_anchor("pin_rt")
    assert anchor is not None
    assert anchor["text"] == "roundtrip rule"
    assert reloaded.last_summary_hash == "abc123"
    assert not reloaded._data.get("self_sha256"), (
        "in-memory schema stays clean; the hash is written at save time only"
    )


def test_session_store_hash_mismatch_warns_but_loads(tmp_path, monkeypatch, caplog):
    """Advisory integrity: mismatch => warning + proceed with loaded data."""
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    store = SessionStore("mm-s")
    store.add_anchor("pin_mm", "mismatch rule", "mismatch", 50, ["mm"])
    store.save()

    raw = _raw_store_file(store)
    raw["anchors"]["pin_mm"]["text"] = "tampered between sessions"
    raw.pop("self_sha256")
    tampered_hash = "0" * 64
    raw["self_sha256"] = tampered_hash
    store._path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING, logger="memlock_core.store"):
        reloaded = SessionStore("mm-s")

    anchor = reloaded.get_anchor("pin_mm")
    assert anchor is not None, "session stores load despite mismatch"
    assert anchor["text"] == "tampered between sessions"
    assert any(
        "self-integrity" in rec.message for rec in caplog.records
    ), caplog.records


def test_session_store_without_self_sha256_still_loads(tmp_path, monkeypatch, caplog):
    """Pre-0.5.0 files (no self_sha256) load silently, no warning."""
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    store = SessionStore("old-s")
    store.add_anchor("pin_old", "legacy rule", "legacy", 50, ["old"])
    raw = _raw_store_file(store)
    raw.pop("self_sha256")
    store._path.write_text(json.dumps(raw))

    with caplog.at_level(logging.WARNING, logger="memlock_core.store"):
        reloaded = SessionStore("old-s")
    assert reloaded.get_anchor("pin_old")["text"] == "legacy rule"
    assert not any("self-integrity" in rec.message for rec in caplog.records)


def test_rollback_persists_through_service_save_roundtrip(monkeypatch, tmp_path):
    """Rollback survives disk round-trip via on_end-style save."""
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    from memlock_core import MemlockService

    svc = MemlockService({})
    svc.on_start(session_id="disk-rb")
    store = svc.get_store("disk-rb")
    assert store is not None
    svc.do_pin(store, {"text": "Disk original"})
    pin_id = next(iter(store.anchors()))
    svc.update_pin(store, pin_id, {"text": "Disk updated"})
    svc.on_end(session_id="disk-rb")

    svc2 = MemlockService({})
    svc2.on_start(session_id="disk-rb")
    store2 = svc2.get_store("disk-rb")
    assert store2 is not None
    out = svc2.rollback_pin(store2, pin_id)
    assert out.startswith(f"Rolled back pin (id={pin_id}")
    restored = store2.get_anchor(pin_id)
    assert restored is not None
    assert restored["text"] == "Disk original"
