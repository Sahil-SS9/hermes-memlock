"""Manifest hardening regression tests for M1-M3."""

import json
import time
from pathlib import Path

import memlock_core.persistence as persistence


def test_manifest_concurrent_writes_preserve_entries(tmp_path):
    """M1: Two sequential manifest writes where second writer's file-set differs
    => first writer's entries not lost.
    """
    pdir = tmp_path / "persist"
    store1 = persistence.FileStore(directory=pdir)

    # First pin
    store1.save_pin({"id": "pin_a", "text": "first", "probes": ["first"]})
    manifest1 = json.loads((pdir / "manifest.json").read_text())
    assert "pin_a" in manifest1["pins"]

    # Second store instance adds a different pin
    store2 = persistence.FileStore(directory=pdir)
    store2.save_pin({"id": "pin_b", "text": "second", "probes": ["second"]})
    manifest2 = json.loads((pdir / "manifest.json").read_text())
    assert "pin_a" in manifest2["pins"], "first writer's entry preserved"
    assert "pin_b" in manifest2["pins"]
    assert len(manifest2["pins"]) == 2


def test_quarantined_file_not_rebaselined(tmp_path, caplog):
    """M3: Tampered file quarantined then save_pin called => tampered bytes
    NOT re-baselined into manifest. The tampered pin keeps its ORIGINAL
    manifest hash (so it stays quarantined on every later load); only the
    genuinely new/changed pins get fresh entries.
    """
    pdir = tmp_path / "persist"
    store = persistence.FileStore(directory=pdir)

    # Create a clean pin (manifest now records its original hash)
    store.save_pin({"id": "pin_good", "text": "good", "probes": ["good"]})
    original_manifest = json.loads((pdir / "manifest.json").read_text())
    original_hash = original_manifest["pins"]["pin_good"]["sha256"]

    # Tamper with the file AFTER manifest write
    pin_path = pdir / "pin_good.json"
    data = json.loads(pin_path.read_text())
    data["text"] = "tampered content"
    pin_path.write_text(json.dumps(data, indent=2))

    # load_pins() must quarantine the tampered file...
    pins = store.load_pins()
    assert all(p["id"] != "pin_good" for p in pins), (
        "tampered pin was loaded instead of quarantined"
    )

    # ...and a subsequent save must NOT re-baseline the tampered bytes.
    store.save_pin({"id": "pin_new", "text": "new", "probes": ["new"]})

    manifest = json.loads((pdir / "manifest.json").read_text())
    # Good pin's entry still carries its ORIGINAL hash — the tampered bytes
    # were never silently accepted into the baseline.
    good_entry = manifest["pins"]["pin_good"]
    assert good_entry["sha256"] == original_hash
    # New pin present with valid hash
    assert manifest["pins"]["pin_new"]["sha256"]
    assert set(manifest["pins"].keys()) == {"pin_good", "pin_new"}

    # And the tampered file is STILL quarantined after the re-save.
    pins_after = store.load_pins()
    assert all(p["id"] != "pin_good" for p in pins_after)