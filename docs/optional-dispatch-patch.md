# optional-dispatch-patch.md

## Forwarding `session_id` to plugin tool handlers

Vanilla Hermes Agent's `registry.dispatch()` does not forward `session_id`
to tool handler callbacks. Plugin tools like `guard_pin` receive only the
invocation arguments in the `args` dict — they have no direct way to know
which session called them.

### The patch (3 lines in `model_tools.py`)

In the `handle_function_call` or equivalent function (typically
`model_tools.py` around line 1120), two `registry.dispatch()` calls exist —
one for `execute_code` and one for the normal tool path. Add
`session_id=session_id or ""` to both:

```python
def _dispatch(next_args: Dict[str, Any]) -> Any:
    return registry.dispatch(
        function_name, next_args,
        task_id=task_id,
        user_task=user_task,
        session_id=session_id or "",   # add this line
    )
```

And for the `execute_code` path:

```python
def _dispatch(next_args: Dict[str, Any]) -> Any:
    return registry.dispatch(
        function_name, next_args,
        task_id=task_id,
        user_task=user_task,
        session_id=session_id or "",   # add this line
    )
```

### Behaviour without the patch

Plugin handlers fall back to the **last-seen session**:

```python
session_id = kwargs.get("session_id", "") or _current_session_id
```

In single-session environments (CLI, personal use), `_current_session_id`
is always correct. Under concurrent gateway sessions (multiple users on
Discord), the last-seen session may not be the calling session. This is a
documented race hazard — the fix is the 3-line patch above, which is
tracked for upstream contribution to NousResearch.
