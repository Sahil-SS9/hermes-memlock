"""Setup wizard (memlock_setup.py) tests.

The script is driven three ways:
  - in-process via main([...]) for detection and exit-code assertions;
  - via a plain subprocess with a HERMES_HOME env override for CLI paths
    that must work without PyYAML (dry run, prune, refusal paths);
  - via ``uv run --with pyyaml`` subprocesses for the real config-merge
    paths — the script deliberately refuses to edit config.yaml when
    PyYAML is missing, so proving merge semantics needs yaml present.
    Those tests skip (not fail) if the uv sandbox cannot provide pyyaml.

All filesystem effects land under per-test tmp dirs — never ~/.hermes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO / "memlock_setup.py"


def _run_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    """Run memlock_setup.py as a real subprocess against a temp HERMES_HOME."""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


_UV = shutil.which("uv")


def _run_cli_yaml(home: Path, *args: str):
    """Same, but under an interpreter that definitely has PyYAML."""
    if _UV is None:
        pytest.skip("uv unavailable: cannot provision pyyaml for merge test")
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        [_UV, "run", "--with", "pyyaml", "--python", "3.12", "--no-project",
         "python", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.fixture
def home(tmp_path):
    """Fresh HERMES_HOME dir with a minimal non-memlock config.yaml.

    Named setup_home (not hermes_home) because conftest.py's autouse
    isolated_hermes_home fixture already owns tmp_path/hermes_home.
    """
    h = tmp_path / "setup_home"
    h.mkdir()
    # Written in PyYAML safe_dump canonical style (2-space indent, list items
    # aligned with their key) so test_non_memlock_config_survives_merge can
    # assert byte-level prefix preservation after a real merge.
    (h / "config.yaml").write_text(
        "plugins:\n"
        "  enabled:\n"
        "  - memlock\n"
        "model:\n"
        "  provider: opencode-go\n"
        "  temperature: 0.7\n"
    )
    return h


# ── detection matrix ─────────────────────────────────────────────────────


def test_detect_severian_only_dir(home):
    (home / "plugins" / "severian").mkdir(parents=True)
    (home / "plugins" / "severian" / "plugin.yaml").write_text("name: severian\n")
    import memlock_setup as ms

    verdict, findings = ms.detect_provider(home)
    assert verdict == "severian"
    assert any("Severian plugin dir" in f for f in findings)


def test_detect_mnemosyne_only(home):
    # plugin dir alone is enough for the mnemosyne verdict
    (home / "plugins" / "mnemosyne").mkdir(parents=True)
    import memlock_setup as ms

    verdict, findings = ms.detect_provider(home)
    assert verdict == "mnemosyne"
    assert any("Mnemosyne plugin dir" in f for f in findings)


def test_detect_mnemosyne_via_enabled_config(home):
    cfg = (
        "plugins:\n"
        "  enabled:\n"
        "    - mnemosyne\n"
    )
    (home / "config.yaml").write_text(cfg)
    import memlock_setup as ms

    verdict, _ = ms.detect_provider(home)
    assert verdict == "mnemosyne"


def test_detect_both_present_severian_wins(home):
    (home / "plugins" / "severian" / "plugin.yaml").parent.mkdir(parents=True)
    (home / "plugins" / "severian" / "plugin.yaml").write_text("name: severian\n")
    (home / "plugins" / "mnemosyne").mkdir(parents=True)
    import memlock_setup as ms

    verdict, findings = ms.detect_provider(home)
    assert verdict == "severian", "severian must win when both are present"
    # honest report lists BOTH providers' evidence
    assert any("Mnemosyne plugin dir" in f for f in findings)


def test_detect_none(home):
    import memlock_setup as ms

    verdict, findings = ms.detect_provider(home)
    assert verdict == "none"
    assert len(findings) >= 4  # all probes reported honestly


def test_detect_db_file_alone_is_not_verdict(home):
    """A stray DB file without plugin dir/enabled entry stays 'none'.

    The db file is report-only evidence: mnemosyne_detected requires the
    plugin dir or an enabled-config listing.
    """
    dbdir = home / "mnemosyne" / "data"
    dbdir.mkdir(parents=True)
    (dbdir / "mnemosyne.db").write_bytes(b"sqlite")
    import memlock_setup as ms

    verdict, findings = ms.detect_provider(home)
    assert verdict == "none"
    assert any("database file" in f for f in findings)  # but it IS reported


# ── backup + config merge ────────────────────────────────────────────────


def test_backup_created_with_timestamp_suffix_and_merge_scoped(home):
    before = (home / "config.yaml").read_bytes()
    proc = _run_cli_yaml(home, "--provider", "mnemosyne")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    backups = list(home.glob("config.yaml.bak-*"))
    assert len(backups) == 1, "exactly one timestamped backup expected"
    stamp = backups[0].name.removeprefix("config.yaml.bak-")
    time.strptime(stamp, "%Y%m%d-%H%M%S")  # raises if not that exact shape
    assert backups[0].read_bytes() == before, "backup is a byte copy of pre-state"

    merged_text = (home / "config.yaml").read_text()
    assert "reverse_audit: true" in merged_text
    assert "preference_adapter: mnemosyne" in merged_text


def test_non_memlock_config_survives_merge(home):
    before = (home / "config.yaml").read_text()
    proc = _run_cli_yaml(home, "--provider", "severian")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    after = (home / "config.yaml").read_text()
    # non-memlock regions byte-identical: original content preserved verbatim
    assert after.startswith(before), (
        "non-memlock config regions must be byte-identical after the merge"
    )
    tail = after.removeprefix(before)
    assert tail.lstrip().startswith("memlock:"), "only the memlock section added"
    assert "preference_adapter: severian" in tail
    assert "reverse_audit: true" in tail


def test_merge_preserves_existing_memlock_keys(home):
    (home / "config.yaml").write_text(
        "memlock:\n"
        "  drift_threshold: 0.9\n"
        "  anchors:\n"
        "    - id: keep-me\n"
        "      text: stay\n"
        "  preference_adapter: \"\"\n"
    )
    proc = _run_cli_yaml(home, "--provider", "mnemosyne")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    text = (home / "config.yaml").read_text()
    # untouched user keys survive...
    assert "drift_threshold: 0.9" in text
    assert "id: keep-me" in text
    assert "text: stay" in text
    # ...only wizard-owned keys change
    assert "preference_adapter: mnemosyne" in text
    assert 'preference_adapter: ""' not in text


def _run_cli_no_yaml(home: Path, *args: str) -> tuple[int, str]:
    """Run memlock_setup.py with ``import yaml`` forced to fail.

    sys.modules['yaml'] = None makes any later ``import yaml`` raise
    ImportError, simulating a PyYAML-less install regardless of what the
    parent interpreter has available.
    """
    runner = (
        "import sys\n"
        f"sys.path.insert(0, {str(_REPO)!r})\n"
        "sys.modules['yaml'] = None  # simulate a PyYAML-less install\n"
        f"sys.argv = [{str(_SCRIPT)!r}] + {list(args)!r}\n"
        "code = 0\n"
        "try:\n"
        "    src = open(" + repr(str(_SCRIPT)) + ").read()\n"
        "    exec(compile(src, " + repr(str(_SCRIPT)) +
        ", 'exec'), {'__name__': '__main__'})\n"
        "except SystemExit as e:\n"
        "    code = e.code if isinstance(e.code, int) else 0\n"
        "print('EXIT_CODE=%d' % code)\n"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        capture_output=True, text=True, env=env, timeout=60,
    )
    lines = proc.stdout.splitlines()
    code_line = next((ln for ln in lines if ln.startswith("EXIT_CODE=")), "")
    return int(code_line.split("=")[1]) if code_line else -1, proc.stdout


def test_no_pyyaml_refuses_to_edit_but_still_backs_up(home):
    """The stdlib-only failure path: guidance instead of a mangled config."""
    before = (home / "config.yaml").read_bytes()
    rc, out = _run_cli_no_yaml(home, "--provider", "mnemosyne")
    assert "PyYAML not available" in out
    assert "Manual step" in out
    assert rc == 0  # explicit provider override still counts as applied=ok
    # config untouched; backup was taken first
    assert (home / "config.yaml").read_bytes() == before
    assert len(list(home.glob("config.yaml.bak-*"))) == 1

    # auto-detect mode on nothing-installed signals the same refusal with 1
    empty_home = home.parent / "empty_home2"
    empty_home.mkdir()
    rc2, out2 = _run_cli_no_yaml(empty_home)
    assert rc2 == 1
    assert "PyYAML not available" in out2


# ── dry run ──────────────────────────────────────────────────────────────


def test_dry_run_changes_nothing(home):
    before_text = (home / "config.yaml").read_text()
    before_bytes = (home / "config.yaml").read_bytes()

    proc = _run_cli(home, "--dry-run", "--provider", "severian")
    assert proc.returncode == 0
    out = proc.stdout
    assert "Dry run" in out
    assert "preference_adapter: severian" in out
    assert "reverse_audit: true" in out

    assert (home / "config.yaml").read_bytes() == before_bytes, \
        "dry run must not touch config.yaml"
    assert not list(home.glob("*.bak-*")), "dry run writes no backup either"

    # dry run on a MISSING config also changes nothing and notes it
    empty_home = home.parent / "empty_home"
    empty_home.mkdir()
    proc2 = _run_cli(empty_home, "--dry-run")
    assert proc2.returncode in (0, 1)
    assert "does not exist yet" in proc2.stdout
    assert not (empty_home / "config.yaml").exists()


# ── pruning ──────────────────────────────────────────────────────────────


def test_prune_deletes_old_files_but_never_persist(home):
    memlock_dir = home / "memlock"
    persist = memlock_dir / "persist"
    persist.mkdir(parents=True)

    old_json = memlock_dir / "old-session.json"
    old_json.write_text('{"session_id": "old"}')
    ancient = time.time() - 40 * 86400
    os.utime(old_json, (ancient, ancient))

    recent_json = memlock_dir / "recent-session.json"
    recent_json.write_text('{"session_id": "recent"}')  # mtime = now

    durable_pin = persist / "pin_1.json"
    durable_pin.write_text(json.dumps({"id": "pin_1", "text": "durable"}))
    os.utime(durable_pin, (ancient, ancient))  # OLD but inside persist/

    not_json = memlock_dir / "notes.txt"
    not_json.write_text("keep me")
    os.utime(not_json, (ancient, ancient))

    rc = _SCRIPT_MAIN(["--prune-days", "30", "--provider", "mnemosyne"], home)
    assert rc == 0

    assert not old_json.exists(), "old session store deleted"
    assert recent_json.exists(), "fresh session store kept"
    assert durable_pin.exists(), "persist/ content NEVER pruned"
    assert not_json.exists(), "non-.json files untouched"


def test_prune_days_below_7_rejected(home):
    proc = _run_cli(home, "--prune-days", "6")
    assert proc.returncode == 2
    combined = proc.stdout + proc.stderr
    assert ">= 7" in combined
    # nothing was deleted or written
    assert (home / "config.yaml").exists()


def test_prune_dry_run_reports_without_deleting(home):
    memlock_dir = home / "memlock"
    memlock_dir.mkdir()
    victim = memlock_dir / "ancient.json"
    victim.write_text("{}")
    ancient = time.time() - 100 * 86400
    os.utime(victim, (ancient, ancient))

    proc = _run_cli(home, "--prune-days", "30", "--dry-run",
                    "--provider", "mnemosyne")
    assert proc.returncode == 0
    assert "would delete" in proc.stdout
    assert victim.exists(), "dry-run prune deletes nothing"

    # and the real prune then removes exactly it (explicit --provider keeps
    # the combined prune+detect run at exit 0 despite no provider installed)
    rc = _SCRIPT_MAIN(["--prune-days", "30", "--provider", "mnemosyne"], home)
    assert rc == 0
    assert not victim.exists()


# ── exit codes ───────────────────────────────────────────────────────────


def _SCRIPT_MAIN(args: list[str], home: Path) -> int:
    """In-process main() with HERMES_HOME pointed at the tmp dir."""
    saved = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        # import fresh each call so module-level state can't leak between cases
        for mod in [m for m in sys.modules if m.startswith("memlock_setup")]:
            del sys.modules[mod]
        spec = __import__("importlib").util.spec_from_file_location(
            f"memlock_setup_under_{abs(hash(str(home))) % 99999}", str(_SCRIPT),
        )
        mod = __import__("importlib").util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.main(list(args))
    finally:
        if saved is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved


def test_exit_code_0_when_provider_detected_and_applied(home):
    (home / "plugins" / "mnemosyne").mkdir(parents=True)
    assert _SCRIPT_MAIN([], home) == 0


def test_exit_code_1_when_nothing_detected(home):
    # nothing installed: guidance printed, signal 1
    assert _SCRIPT_MAIN([], home) == 1
    # explicit --provider none still reports detection honestly -> 1
    assert _SCRIPT_MAIN(["--provider", "none"], home) == 1
    # ...but an explicit override onto a DETECTED provider applies cleanly
    assert _SCRIPT_MAIN(["--provider", "severian"], home) == 0


def test_exit_code_2_on_bad_args():
    proc = _run_cli(Path("/tmp"), "--prune-days", "3")
    assert proc.returncode == 2
