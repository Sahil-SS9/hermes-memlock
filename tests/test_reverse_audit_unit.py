from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from detection import SUMMARY_PREFIX, reverse_audit, reverse_audit_unavailable

NOW = "2026-08-04T08:00:00Z"


def memory(mid: str, content: str, **overrides):
    row = {
        "id": mid,
        "content": content,
        "source": "preference",
        "timestamp": "2026-08-01T00:00:00Z",
        "created_at": "2026-08-01T00:00:00Z",
        "valid_until": None,
        "superseded_by": None,
        "scope": "global",
        "metadata_json": {"memlock": {"standing": True}},
        "veracity": "stated",
        "memory_type": "preference",
    }
    row.update(overrides)
    return row


def test_empty_candidate_set_is_ok():
    assert reverse_audit([], [], now=NOW) == {
        "status": "ok",
        "findings": [],
        "rehydrate_ids": [],
        "suppressed_ids": [],
        "errors": [],
    }


def test_present_and_material_absent_are_distinguished():
    rows = [
        memory("present", "Use British English"),
        memory("absent", "Ask questions one at a time"),
    ]
    report = reverse_audit(rows, [{"role": "system", "content": "Use British English."}], now=NOW)
    by_id = {f["canonical_id"]: f for f in report["findings"]}
    assert by_id["present"]["verdict"] == "PRESENT"
    assert by_id["absent"]["verdict"] == "ABSENT"
    assert by_id["absent"]["reason"] == "no supporting or contradicting evidence in active context"
    assert report["rehydrate_ids"] == ["absent"]


def test_absent_non_material_preference_is_not_rehydrated():
    row = memory("bio", "Prefers jazz", metadata_json={})
    report = reverse_audit([row], [], now=NOW)
    assert report["findings"][0]["verdict"] == "ABSENT"
    assert report["findings"][0]["material"] is False
    assert report["rehydrate_ids"] == []


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"valid_until": NOW}, "stored preference expired"),
        ({"superseded_by": "new-id"}, "stored preference is superseded"),
    ],
)
def test_stale_rows_are_suppressed(overrides, reason):
    row = memory("old", "Use concise answers", **overrides)
    report = reverse_audit([row], [], now=NOW)
    finding = report["findings"][0]
    assert finding["verdict"] == "STORED_STALE"
    assert finding["reason"] == reason
    assert report["suppressed_ids"] == ["old"]


def test_exact_duplicates_collapse_with_deterministic_newest_canonical_id():
    rows = [
        memory("z-old", "Use concise answers", timestamp="2026-07-01T00:00:00Z"),
        memory("a-new", "Use concise answers", timestamp="2026-08-02T00:00:00Z"),
        memory("b-new", "Use concise answers", timestamp="2026-08-02T00:00:00Z"),
    ]
    report = reverse_audit(rows, [], now=NOW)
    assert len(report["findings"]) == 1
    finding = report["findings"][0]
    assert finding["memory_ids"] == ["a-new", "b-new", "z-old"]
    assert finding["canonical_id"] == "a-new"
    assert report["rehydrate_ids"] == ["a-new"]


def test_conflicting_live_values_are_ambiguous_and_all_suppressed():
    metadata_a = {"memlock": {"preference_key": "language", "value": "English", "standing": True}}
    metadata_b = {"memlock": {"preference_key": "language", "value": "French", "standing": True}}
    report = reverse_audit(
        [memory("en", "Use English", metadata_json=metadata_a), memory("fr", "Use French", metadata_json=metadata_b)],
        [], now=NOW,
    )
    assert len(report["findings"]) == 1
    assert report["findings"][0]["verdict"] == "AMBIGUOUS"
    assert report["findings"][0]["memory_ids"] == ["en", "fr"]
    assert report["suppressed_ids"] == ["en", "fr"]
    assert report["rehydrate_ids"] == []


def test_explicit_newer_user_value_contradicts_stored_value():
    metadata = {"memlock": {"preference_key": "language", "value": "English", "standing": True}}
    report = reverse_audit(
        [memory("en", "Use English", metadata_json=metadata)],
        [{"role": "user", "content": "language: French", "timestamp": "2026-08-03T00:00:00Z"}],
        now=NOW,
    )
    finding = report["findings"][0]
    assert finding["verdict"] == "CONTRADICTED"
    assert finding["evidence"] == ["language: French"]
    assert report["suppressed_ids"] == ["en"]


def test_malformed_rows_degrade_without_hiding_valid_findings():
    report = reverse_audit(
        [{"id": "", "content": "broken"}, memory("ok", "Use British English")],
        [], now=NOW,
    )
    assert report["status"] == "degraded"
    assert [f["verdict"] for f in report["findings"]] == ["UNKNOWN", "ABSENT"]
    assert report["errors"] == [{"code": "malformed_memory", "memory_id": "", "reason": "missing id or blank content"}]


def test_malformed_metadata_is_unknown_not_absent():
    report = reverse_audit([memory("bad-meta", "Use concise answers", metadata_json="not-json")], [], now=NOW)
    assert report["status"] == "degraded"
    assert report["findings"][0]["verdict"] == "UNKNOWN"
    assert report["rehydrate_ids"] == []


def test_provider_failure_report_is_stable_and_fail_open():
    assert reverse_audit_unavailable(RuntimeError("secret provider detail")) == {
        "status": "unavailable",
        "findings": [],
        "rehydrate_ids": [],
        "suppressed_ids": [],
        "errors": [{"code": "mnemosyne_query_failed", "reason": "preference memory query unavailable"}],
    }


def test_pre_llm_queries_preferences_after_compaction_and_rehydrates_with_budget(memlock):
    calls = []

    def provider(query, limit):
        calls.append((query, limit))
        return [memory(
            "pref-question",
            "Ask questions one at a time",
            metadata_json={"memlock": {"standing": True, "priority": 90, "probes": ["one at a time"]}},
        )]

    memlock._cfg = {
        "reverse_audit": True,
        "reverse_preference_query": "applicable user preferences",
        "reverse_limit": 25,
        "max_slots": 1,
        "max_reminder_chars": 200,
        "inject": "on-drift",
        "hard_reinject_turns": 40,
    }
    memlock.set_reverse_preference_provider(provider)
    history = [
        {"role": "system", "content": "System"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " compacted"},
    ]
    result = memlock._on_pre_llm(
        session_id="reverse-integration", user_message="What next?", conversation_history=history
    )
    assert calls == [("applicable user preferences", 25)]
    assert result == {"context": "[Standing instructions — still active]\n  - Ask questions one at a time"}


def test_pre_llm_provider_failure_leaves_turn_unchanged(memlock):
    def provider(query, limit):
        raise TimeoutError("unavailable")

    memlock._cfg = {"reverse_audit": True, "inject": "on-drift", "hard_reinject_turns": 40}
    memlock.set_reverse_preference_provider(provider)
    history = [
        {"role": "system", "content": "System"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " compacted"},
    ]
    assert memlock._on_pre_llm(
        session_id="reverse-failure", user_message="Hello", conversation_history=history
    ) is None
