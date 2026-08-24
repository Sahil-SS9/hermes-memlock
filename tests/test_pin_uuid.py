"""M7 regression test: pin/unpin/repin within the same second yields distinct
anchor ids (the old timestamp+len scheme collided; uuid suffix must not).
"""

import sys

sys.path.insert(0, "/home/kensei/repos/hermes-memlock")

from memlock_core import MemlockService


def test_pin_id_collision_resistant(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEMLOCK_HOME", str(home))

    svc = MemlockService()
    sid = "m7-sess"
    svc.on_start(sid)
    store = svc.ensure_store(sid)

    # Pin -> unpin -> re-pin immediately (same second): ids must differ.
    r1 = svc.do_pin(store, {"text": "first standing rule"})
    id1 = _extract_id(r1)
    assert id1.startswith("pin_")

    assert store.unpin(id1) is True

    r2 = svc.do_pin(store, {"text": "second standing rule"})
    id2 = _extract_id(r2)
    assert id2.startswith("pin_")

    assert id1 != id2

    # Both ids remain distinct keys in the store history (no silent clobber):
    # only the second is present as a live anchor.
    anchors = store.anchors()
    assert id1 not in anchors
    assert id2 in anchors


def _extract_id(pin_result: str) -> str:
    """Pull the anchor id out of a do_pin result string."""
    marker = "id="
    start = pin_result.index(marker) + len(marker)
    end = pin_result.index(",", start)
    return pin_result[start:end].strip()
