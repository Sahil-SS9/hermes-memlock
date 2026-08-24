"""M6 regression test: session ids with safe-sid collisions produce different files."""

import json
from pathlib import Path

import memlock_core.store as store_mod


def test_session_ids_with_slash_dot_map_to_different_files(tmp_path, monkeypatch):
    """M6: session ids 'sess/a' vs 'sess.a' produce DIFFERENT store files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MEMLOCK_HOME", str(home))

    # Create two stores with session ids that would collide under old slugging
    sess_a = store_mod.SessionStore("sess/a")
    sess_dot = store_mod.SessionStore("sess.a")

    # Their on-disk paths must be different
    assert sess_a._path != sess_dot._path
    # And the filenames must be different
    assert sess_a._path.name != sess_dot._path.name
    # Specifically: sess/a -> sess_slash_a.json, sess.a -> sess_dot_a.json
    assert sess_a._path.name == "sess_slash_a.json"
    assert sess_dot._path.name == "sess_dot_a.json"

    # Each can save independently without overwriting the other
    sess_a.save()
    sess_dot.save()

    # Both files exist
    assert sess_a._path.exists()
    assert sess_dot._path.exists()
    # And they contain their respective session_id
    a_data = json.loads(sess_a._path.read_text())
    dot_data = json.loads(sess_dot._path.read_text())
    assert a_data["session_id"] == "sess/a"
    assert dot_data["session_id"] == "sess.a"