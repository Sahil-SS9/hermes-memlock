"""Fixture-backed STALE benchmarks for MemLock's reverse audit.

These tests intentionally assert the entire report.  A report-shape regression is
as important as a wrong verdict because callers use the ID lists to decide what
may be rehydrated or suppressed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

import memlock_core.detection as detection

_FIXTURE = Path(__file__).parent / "fixtures" / "reverse_audit_stale.json"


@pytest.fixture(scope="module")
def stale_benchmark() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case_id",
    [
        "contradicted-current-context",
        "still-valid-present",
        "no-relevant-evidence-material",
        "multiple-only-one-violation",
        "duplicate-and-superseded",
        "empty-mnemosyne-response",
    ],
)
def test_reverse_audit_stale_benchmark_complete_report(stale_benchmark, case_id):
    case = next(item for item in stale_benchmark["cases"] if item["id"] == case_id)

    report = detection.reverse_audit(
        case["memories"],
        case["active_region"],
        now=stale_benchmark["now"],
    )

    assert report == case["expected"]


def _query_boundary(
    query: Callable[[], list[dict]],
    active_region: list[dict],
    *,
    now: str,
) -> dict:
    """Minimal deterministic stand-in for the future Mnemosyne adapter boundary.

    The pure reverse_audit() contract performs no I/O.  This harness locks down
    the required fail-open report for the caller that owns retrieval without
    coupling the benchmark to Mnemosyne internals or a live provider.
    """
    try:
        memories = query()
    except Exception as exc:  # the boundary must preserve the Hermes turn
        return {
            "status": "unavailable",
            "findings": [],
            "rehydrate_ids": [],
            "suppressed_ids": [],
            "errors": [
                {"code": "mnemosyne_query_failed", "reason": str(exc)}
            ],
        }
    return detection.reverse_audit(memories, active_region, now=now)


def test_query_failure_is_fail_open_and_structured(stale_benchmark):
    failure = stale_benchmark["query_failure"]

    def failed_query() -> list[dict]:
        raise RuntimeError(failure["exception"])

    report = _query_boundary(
        failed_query,
        [{"role": "user", "content": "Continue the turn"}],
        now=stale_benchmark["now"],
    )

    assert report == failure["expected"]


def test_forward_only_blind_spot_passes_while_reverse_audit_finds_dependency(
    stale_benchmark,
):
    """A Mnemosyne-only preference is invisible to the anchor-first audit."""
    case = next(
        item
        for item in stale_benchmark["cases"]
        if item["id"] == "no-relevant-evidence-material"
    )

    # Existing verification starts from configured anchors.  No anchor exists
    # for this stored preference, so the forward pass reports no drift.
    alive_ids, drifted_ids = detection.audit_anchors([], case["active_region"])
    assert {"alive_ids": alive_ids, "drifted_ids": drifted_ids} == {
        "alive_ids": [],
        "drifted_ids": [],
    }

    reverse_report = detection.reverse_audit(
        case["memories"],
        case["active_region"],
        now=stale_benchmark["now"],
    )
    assert reverse_report == case["expected"]
    assert reverse_report["rehydrate_ids"] == ["pref-concise"]
