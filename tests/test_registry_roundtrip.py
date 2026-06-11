"""Tests for MemLock: plugin registration surface."""


def test_register_wires_hooks_tool_and_command(memlock, fake_ctx_cls, base_cfg):
    """register() attaches the three hooks, the guard_pin tool and /guard."""
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)

    assert set(fc.hooks) == {"on_session_start", "pre_llm_call", "on_session_end"}
    assert "guard_pin" in fc.tools
    assert "guard" in fc.commands

    schema = fc.tools["guard_pin"]["schema"]
    assert schema["name"] == "guard_pin"
    props = schema["parameters"]["properties"]
    assert {"text", "unpin", "reminder", "priority", "probes"} <= set(props)


def test_pin_roundtrip_through_registered_handler(memlock, fake_ctx_cls, base_cfg):
    """A pin made through the registered tool handler lands in the store."""
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id="test-s")

    handler = fc.tools["guard_pin"]["handler"]
    out = handler({"text": "Reply in British English", "priority": 70})
    assert "Pinned instruction" in out

    store = memlock._ensure_store("test-s")
    assert any("British" in a["text"] for a in store.anchors().values())
