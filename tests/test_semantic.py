"""Tests for MemLock: windowed semantic audit (stub model, no downloads)."""
import detection


class StubModel:
    """Embedding stub: maps known phrases to fixed unit vectors.

    Unknown text gets an orthogonal vector, so similarity is 1.0 for an
    exact phrase match inside a window and 0.0 otherwise.
    """

    def __init__(self, known_phrases):
        self.known = list(known_phrases)
        self.encode_calls = 0

    def _vec(self, text):
        dims = len(self.known) + 1
        v = [0.0] * dims
        for i, phrase in enumerate(self.known):
            if phrase in text:
                v[i] = 1.0
                return v
        v[-1] = 1.0
        return v

    def encode(self, texts):
        self.encode_calls += 1
        if isinstance(texts, str):
            texts = [texts]
        return [self._vec(t) for t in texts]


def _with_stub(monkeypatch, stub):
    monkeypatch.setattr(detection, "_semantic_model", stub)


def test_semantic_alive_when_any_window_matches(monkeypatch):
    stub = StubModel(["British English"])
    _with_stub(monkeypatch, stub)
    anchors = [{"id": "a1", "text": "British English"}]
    region = [
        {"role": "user", "content": "Unrelated chatter"},
        {"role": "assistant", "content": "Reply in British English please"},
    ]
    alive, drifted = detection.semantic_audit_anchors(
        anchors, region, sim_threshold=0.65
    )
    assert alive == ["a1"]
    assert drifted == []


def test_semantic_drifted_when_no_window_matches(monkeypatch):
    stub = StubModel(["British English"])
    _with_stub(monkeypatch, stub)
    anchors = [{"id": "a1", "text": "British English"}]
    region = [{"role": "user", "content": "Totally unrelated"}]
    alive, drifted = detection.semantic_audit_anchors(
        anchors, region, sim_threshold=0.65
    )
    assert alive == []
    assert drifted == ["a1"]


def test_semantic_long_message_is_chunked(monkeypatch):
    """The anchor phrase sits deep inside one long message; chunking must
    surface it in some window."""
    stub = StubModel(["British English"])
    _with_stub(monkeypatch, stub)
    anchors = [{"id": "a1", "text": "British English"}]
    long_text = ("padding " * 300) + "use British English here" + (" padding" * 300)
    region = [{"role": "assistant", "content": long_text}]
    alive, drifted = detection.semantic_audit_anchors(
        anchors, region, sim_threshold=0.65, window_chars=500
    )
    assert alive == ["a1"]


def test_semantic_falls_back_to_keyword_without_model(monkeypatch):
    monkeypatch.setattr(detection, "_semantic_model", None)
    monkeypatch.setattr(detection, "_ensure_semantic_model", lambda name: None)
    anchors = [{"id": "a1", "text": "x", "probes": ["bullets"]}]
    region = [{"role": "user", "content": "use bullets"}]
    alive, drifted = detection.semantic_audit_anchors(anchors, region)
    assert alive == ["a1"]  # keyword path found the probe


def test_semantic_windows_batch_encoded_once(monkeypatch):
    """Windows and anchors are each encoded in one batch call, not per item."""
    stub = StubModel(["British English"])
    _with_stub(monkeypatch, stub)
    anchors = [
        {"id": "a1", "text": "British English"},
        {"id": "a2", "text": "bullet points"},
    ]
    region = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    detection.semantic_audit_anchors(anchors, region, sim_threshold=0.65)
    assert stub.encode_calls == 2  # one for windows, one for anchors


def test_build_windows_overlap():
    """Chunks of a long message overlap so edge-spanning text is retained."""
    text = "x" * 2500
    windows = detection._build_windows(
        [{"role": "user", "content": text}], window_chars=1000
    )
    assert len(windows) >= 3
    assert all(len(w) <= 1000 for w in windows)
    # Overlap means total reconstructed length exceeds the original
    assert sum(len(w) for w in windows) > len(text)


def test_empty_region_drifts_everything(monkeypatch):
    stub = StubModel(["anything"])
    _with_stub(monkeypatch, stub)
    anchors = [{"id": "a1", "text": "anything"}]
    alive, drifted = detection.semantic_audit_anchors(anchors, [])
    assert drifted == ["a1"]
