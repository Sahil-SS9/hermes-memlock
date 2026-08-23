"""Adapter layer tests — no real network, no real database.

Severian's provider is exercised with a monkeypatched ``connect`` callable
(the module resolves ``psycopg2.connect`` / ``psycopg.connect`` lazily inside
the query), and Mnemosyne's against fabricated tmp sqlite files that carry
exactly the ``working_memory`` schema shape its source queries. Every
expected failure mode must degrade to an empty list or a skipped row —
never an exception escaping to the pre_llm_call hook.
"""
from __future__ import annotations

import json
import sqlite3

import memlock_adapters as ad
from memlock_adapters import mnemosyne as mnem
from memlock_adapters import severian as sev


# ── helpers ───────────────────────────────────────────────────────────────


def _make_working_memory_db(path, rows):
    """Fabricate a Mnemosyne-shaped DB: working_memory table + rows.

    Column set mirrors what memlock_adapters/mnemosyne.py reads (SELECT *
    via cursor.description): id, content, source, created_at, timestamp,
    valid_until, superseded_by, scope, metadata_json, veracity,
    memory_type, plus the meta keys it copies into metadata_json.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE working_memory (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                timestamp REAL,
                created_at TEXT,
                valid_until TEXT,
                superseded_by TEXT,
                superseded_by_id TEXT,
                scope TEXT,
                metadata_json TEXT,
                veracity TEXT,
                validator TEXT,
                memory_type TEXT,
                priority REAL,
                probes TEXT,
                standing TEXT,
                material INTEGER,
                preference_key TEXT,
                value TEXT
            )
            """
        )
        cols = (
            "id, content, source, timestamp, created_at, valid_until, "
            "superseded_by, scope, metadata_json, veracity, memory_type"
        )
        for r in rows:
            conn.execute(
                f"INSERT INTO working_memory ({cols}) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.get("id"), r.get("content"), r.get("source"),
                    r.get("timestamp"), r.get("created_at"),
                    r.get("valid_until"), r.get("superseded_by"),
                    r.get("scope"), r.get("metadata_json"),
                    r.get("veracity"), r.get("memory_type"),
                ),
            )
            # priority is a separate column (not in the shared insert
            # above); set it explicitly for rows that carry one.
            if r.get("priority") is not None:
                conn.execute(
                    "UPDATE working_memory SET priority = ? WHERE id = ?",
                    (r["priority"], r["id"]),
                )
        conn.commit()
    finally:
        conn.close()


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.description = (("id",), ("record_type",), ("status",), ("payload",))

    def execute(self, query, params=None):
        self.executed_query = query
        self.executed_params = params

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Context-manager stand-in for a psycopg connection."""

    def __init__(self, cur):
        self.cur = cur
        self.closed_during_with = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        class _CurCtx:
            def __init__(self, c):
                self._c = c

            def __enter__(self):
                return self._c

            def __exit__(self, *exc):
                return False

        return _CurCtx(self.cur)


def _fake_psycopg2(monkeypatch, connect_impl):
    """Install a fake psycopg2 whose .connect is ``connect_impl``."""
    import types

    fake = types.ModuleType("psycopg2")
    fake.connect = connect_impl
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", fake)


# ── mnemosyne adapter ─────────────────────────────────────────────────────


def test_mnemosyne_maps_rows_from_fabricated_sqlite(tmp_path):
    db = tmp_path / "mnemosyne.db"
    _make_working_memory_db(db, [
        {
            "id": "mem-1",
            "content": "User prefers bullet points in every reply",
            "source": "user-stated",
            # epoch seconds must become ISO-8601 UTC for downstream parsing
            "timestamp": 1755900000.0,
            "scope": "global",
            # priority lands in the working_memory.priority column and is
            # what drives metadata_json assembly ({"memlock": {...}}).
            "priority": 90,
        },
        {
            "id": "mem-2",
            "content": "Always write British English spellings",
            "created_at": "2026-08-01T10:00:00Z",
            "veracity": "confirmed",
            "memory_type": "preference",
        },
    ])
    provider = mnem.make_provider(str(db))
    rows = provider("applicable user preferences", 50)

    assert [r["id"] for r in rows] == ["mem-2", "mem-1"]  # rowid DESC
    by_id = {r["id"]: r for r in rows}
    assert "bullet" in by_id["mem-1"]["content"]
    # epoch → ISO conversion
    assert by_id["mem-1"]["timestamp"].endswith("Z")
    assert by_id["mem-1"]["memory_type"] == "working_memory"
    # meta keys assembled into metadata_json under {"memlock": ...}
    meta = json.loads(by_id["mem-1"]["metadata_json"])
    assert meta == {"memlock": {"priority": 90}}
    # veracity passthrough on the second row
    assert by_id["mem-2"]["veracity"] == "confirmed"


def test_mnemosyne_malformed_rows_are_skipped_without_raising(tmp_path):
    db = tmp_path / "mnemosyne.db"
    _make_working_memory_db(db, [
        {"id": "", "content": "no id"},          # unusable: no id
        {"id": "m-blank", "content": ""},         # unusable: no content
        {"id": "m-good", "content": "A usable preference row"},
    ])
    provider = mnem.make_provider(str(db))
    rows = provider("q", 50)
    assert [r["id"] for r in rows] == ["m-good"]


def test_severian_connect_failure_fails_open(monkeypatch):
    calls = []

    def boom(dsn, *a, **kw):
        calls.append(dsn)
        raise ConnectionError("database unreachable")

    _fake_psycopg2(monkeypatch, boom)
    provider = sev.make_provider("postgresql://user@localhost/sev")
    rows = provider("applicable user preferences", 25)
    assert rows == []
    assert calls == ["postgresql://user@localhost/sev"]  # DSN override won


def test_severian_happy_path_via_fake_cursor(monkeypatch):
    payload = {
        "content": "Never deploy on Friday",
        "source": {"kind": "user-stated"},
        "created_at": "2026-07-01T09:00:00Z",
        "superseded_by_id": None,
        "scope": "global",
        "veracity": "confirmed",
        "memlock": {"priority": 70},
    }
    cur = _FakeCursor([
        ("rec-1", "memory", "active", json.dumps(payload)),
        # already-decoded dict payload (psycopg2 json→dict) must work too
        ("rec-2", "observation", "active",
         {"content": "Prefers terse answers", "created_at":
          "2026-06-01T00:00:00Z"}),
        # malformed row: no content -> skipped, batch continues
        ("rec-3", "memory", "active", {}),
    ])
    captured = {}

    def fake_connect(dsn, *a, **kw):
        captured["dsn"] = dsn
        return _FakeConn(cur)

    _fake_psycopg2(monkeypatch, fake_connect)
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)
    provider = sev.make_provider("postgresql://memlock-test/happy")
    rows = provider("preferences please", 40)

    assert captured["dsn"] == "postgresql://memlock-test/happy"
    assert [r["id"] for r in rows] == ["rec-1", "rec-2"]
    assert rows[0]["content"] == "Never deploy on Friday"
    assert rows[0]["source"] == "user-stated"      # dict source.kind flattened
    assert rows[0]["superseded_by"] is None
    assert rows[0]["memory_type"] == "memory"
    assert json.loads(rows[0]["metadata_json"])["memlock"]["priority"] == 70
    # limit forwarded, clamped ≥ 1
    assert cur.executed_params == (list(sev._RECORD_TYPES), 40)


def test_unknown_adapter_name_returns_none():
    assert ad.get_provider("does-not-exist") is None
    assert ad.get_provider("") is None
    assert "does-not-exist" not in ad.available_adapters()


def test_explicit_registration_precedes_config_adapter(
    monkeypatch, isolated_hermes_home,
):
    """set_reverse_preference_provider() wins over config-selected adapters."""
    import memlock as ml

    explicit_rows = [{"id": "x1", "content": "explicit registration wins"}]

    def explicit_provider(query, limit):
        return explicit_rows

    def factory_should_never_run():
        raise AssertionError("config adapter factory must not be used when "
                             "an explicit provider is registered")

    ad.register_adapter("severian", factory_should_never_run)
    try:
        ml._cfg.update({
            "reverse_audit": True,
            "preference_adapter": "severian",
        })
        ml.set_reverse_preference_provider(explicit_provider)
        ml._reset_preference_adapter_cache()

        resolved = ml._resolve_preference_provider()
        assert resolved is explicit_provider

        candidates, report = ml._run_reverse_audit([])
        assert candidates == []  # no active region overlap asserted here;
        assert report["status"] == "ok"
        assert explicit_rows  # provider value untouched by the audit call
    finally:
        ml.set_reverse_preference_provider(None)
        ml._reset_preference_adapter_cache()
        # restore the built-in factories for other tests
        ad.register_adapter(
            "severian",
            lambda: __import__(
                "memlock_adapters.severian", fromlist=["get_preference_provider"]
            ).get_preference_provider,
        )


def test_normalise_row_handles_missing_keys_none_and_nondict():
    # non-dict raw input
    assert ad.normalise_row(None) is None
    assert ad.normalise_row("just a string") is None
    assert ad.normalise_row(42) is None
    assert ad.normalise_row(["id", "content"]) is None

    # missing/None required fields
    assert ad.normalise_row({}) is None
    assert ad.normalise_row({"id": "x"}) is None              # no content
    assert ad.normalise_row({"content": "y"}) is None         # no id
    assert ad.normalise_row({"id": "  ", "content": "y"}) is None
    assert ad.normalise_row({"id": "x", "content": "   "}) is None
    assert ad.normalise_row({"id": None, "content": None}) is None

    # minimal valid row: optional columns default to None, never KeyError
    row = ad.normalise_row({"id": "ok-1", "content": "fine"})
    assert row == {
        "id": "ok-1", "content": "fine", "source": None,
        "timestamp": None, "created_at": None, "valid_until": None,
        "superseded_by": None, "scope": None, "metadata_json": None,
        "veracity": None, "memory_type": None,
    }

    # extra unknown keys are dropped; whitespace stripped
    rich = ad.normalise_row({
        "id": " ok-2 ", "content": " padded ",
        "source": "test", "unexpected_key": "dropped",
    })
    assert rich["id"] == "ok-2" and rich["content"] == "padded"
    assert "unexpected_key" not in rich

    # datetime-like objects coerce to isoformat strings
    class FakeDT:
        def isoformat(self):
            return "2026-08-23T12:00:00"

    dt_row = ad.normalise_row({"id": "d", "content": "c", "timestamp": FakeDT()})
    assert dt_row["timestamp"] == "2026-08-23T12:00:00"


def test_get_provider_factory_crash_fails_open(monkeypatch):
    def bad_factory():
        raise RuntimeError("constructor exploded")

    ad.register_adapter("exploding", bad_factory)
    try:
        assert ad.get_provider("exploding") is None
    finally:
        ad._ADAPTER_FACTORIES.pop("exploding", None)


def test_mnemosyne_missing_db_fails_open(tmp_path):
    provider = mnem.make_provider(str(tmp_path / "nope" / "missing.db"))
    assert provider("q", 10) == []
