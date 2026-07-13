"""Unit tests for the connector's confined local execution (bridge exec relay).

The security property under test: everything the web app relays here — file
syncs AND command working directories — stays inside the dedicated workspace
folder, never the user's wider filesystem.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dcc_bridge.local_exec import LocalExec, WorkspaceViolation  # noqa: E402


def test_safe_path_confines_to_workspace(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    assert ex.safe_path("proj/src/main.py") == (tmp_path / "ws" / "proj" / "src" / "main.py")
    # Empty / root-ish inputs resolve to the workspace itself.
    assert ex.safe_path("") == ex.workspace
    assert ex.safe_path("/") == ex.workspace


@pytest.mark.parametrize("evil", ["../outside.txt", "a/../../b", "..\\..\\windows"])
def test_safe_path_blocks_traversal(tmp_path, evil):
    ex = LocalExec(tmp_path / "ws")
    with pytest.raises(WorkspaceViolation):
        ex.safe_path(evil)


def test_sync_traversal_raises(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    with pytest.raises(WorkspaceViolation):
        ex.sync([{"path": "../escape.py", "content": "nope"}])


def test_sync_happy_path(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    result = ex.sync([
        {"path": "app/main.py", "content": "print('hi')"},
        {"path": "README.md", "content": "# hello"},
    ])
    assert result["written"] == 2
    assert (tmp_path / "ws" / "app" / "main.py").read_text() == "print('hi')"


def test_run_executes_in_confined_cwd(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    (ex.workspace / "sub").mkdir()
    res = ex.run("echo hello", cwd="sub")
    assert res["return_code"] == 0
    assert "hello" in res["stdout"]
    assert res["cwd"] == str(ex.workspace / "sub")


def test_run_falls_back_to_workspace_when_cwd_missing(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    res = ex.run("echo ok", cwd="does/not/exist")
    assert res["return_code"] == 0
    assert res["cwd"] == str(ex.workspace)


def test_run_rejects_traversal_cwd(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    with pytest.raises(WorkspaceViolation):
        ex.run("echo hi", cwd="../..")


# ── host scope (opt-in --host-dir / --allow-any-dir) ─────────────────────────

def test_workspace_scope_never_runs_in_real_host_folder(tmp_path):
    """Without host access an absolute cwd can never address a real host folder:
    it is either confined into the workspace or refused outright (platform-
    dependent path joining) — the command must not run in the real directory."""
    ex = LocalExec(tmp_path / "ws")
    real = tmp_path / "real-project"
    real.mkdir()
    try:
        res = ex.run("echo hi", cwd=str(real))
        assert res["cwd"] != str(real)
        assert res["cwd"].startswith(str(ex.workspace))
    except WorkspaceViolation:
        pass  # refusing is equally safe


def test_host_scope_allows_granted_root(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    res = ex.run("echo hi", cwd=str(proj))
    assert res["return_code"] == 0
    assert res["cwd"] == str(proj)


def test_host_scope_allows_subdir_of_granted_root(tmp_path):
    proj = tmp_path / "proj"
    sub = proj / "svc"
    sub.mkdir(parents=True)
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    res = ex.run("echo hi", cwd=str(sub))
    assert res["cwd"] == str(sub)


def test_host_scope_rejects_outside_roots(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    with pytest.raises(WorkspaceViolation):
        ex.run("echo hi", cwd=str(other))


def test_host_scope_rejects_missing_dir(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    with pytest.raises(WorkspaceViolation):
        ex.run("echo hi", cwd=str(proj / "nope"))


def test_allow_any_dir_accepts_any_existing_dir(tmp_path):
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    ex = LocalExec(tmp_path / "ws", allow_any_dir=True)
    res = ex.run("echo hi", cwd=str(anywhere))
    assert res["cwd"] == str(anywhere)


def test_host_scope_relative_cwd_still_confined(tmp_path):
    """Relative paths keep the historical confined behavior even in host scope."""
    proj = tmp_path / "proj"
    proj.mkdir()
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    with pytest.raises(WorkspaceViolation):
        ex.run("echo hi", cwd="../..")


def test_sync_stays_in_workspace_even_with_host_scope(tmp_path):
    """Host scope grants command cwd only — file syncs never touch host roots."""
    proj = tmp_path / "proj"
    proj.mkdir()
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    result = ex.sync([{"path": "a.txt", "content": "x"}])
    assert (tmp_path / "ws" / "a.txt").exists()
    assert result["workspace"] == str(ex.workspace)
    with pytest.raises(WorkspaceViolation):
        ex.sync([{"path": "../proj/evil.txt", "content": "x"}])


# ── cancellation (web-terminal Ctrl+C) ────────────────────────────────────────

def test_on_spawn_receives_process_and_kill_interrupts(tmp_path):
    """A long-running command dies quickly when its tree is killed via the
    handle passed to on_spawn — the mechanism behind web-terminal Ctrl+C."""
    import threading
    import time

    from dcc_bridge.local_exec import kill_process_tree

    ex = LocalExec(tmp_path / "ws")
    procs = []

    def killer():
        # Wait for spawn, then kill.
        for _ in range(100):
            if procs:
                kill_process_tree(procs[0])
                return
            time.sleep(0.05)

    t = threading.Thread(target=killer)
    t.start()
    started = time.monotonic()
    sleep_cmd = (
        "ping -n 30 127.0.0.1 > NUL" if sys.platform == "win32" else "sleep 30"
    )
    res = ex.run(sleep_cmd, timeout=60, on_spawn=procs.append)
    elapsed = time.monotonic() - started
    t.join()
    assert res["return_code"] != 0
    assert elapsed < 25  # killed long before the command's natural 30s


def test_run_stdin_is_closed_so_prompts_do_not_hang(tmp_path):
    """The relay is non-interactive: a command reading stdin sees EOF at once
    instead of hanging until the timeout."""
    import time

    ex = LocalExec(tmp_path / "ws")
    cmd = (
        "set /p x= && echo got-%x%" if sys.platform == "win32"
        else "read x; echo got-$x"
    )
    started = time.monotonic()
    res = ex.run(cmd, timeout=30)
    assert time.monotonic() - started < 10  # returned immediately, no hang
    assert res["return_code"] is not None


# ── reverse sync (snapshot of command-created files) ─────────────────────────

def test_snapshot_returns_files_created_after_since(tmp_path):
    import time

    ex = LocalExec(tmp_path / "ws")
    (ex.workspace / "old.txt").write_text("old")
    since = time.time() + 5  # nothing yet is newer than this
    (ex.workspace / "railway.json").write_text('{"a": 1}')
    # Only the file whose mtime clears the cutoff (since - epsilon) is eligible;
    # bump the new file's mtime clearly past `since`.
    import os
    os.utime(ex.workspace / "railway.json", (since + 10, since + 10))
    result = ex.snapshot("", since)
    paths = [f["path"] for f in result["files"]]
    assert "railway.json" in paths
    assert "old.txt" not in paths


def test_snapshot_includes_everything_with_since_zero(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    (ex.workspace / "a.py").write_text("print(1)")
    sub = ex.workspace / "pkg"
    sub.mkdir()
    (sub / "b.md").write_text("# doc")
    result = ex.snapshot("", 0.0)
    paths = sorted(f["path"] for f in result["files"])
    assert paths == ["a.py", "pkg/b.md"]
    assert result["count"] == 2
    assert result["truncated"] is False


def test_snapshot_skips_ignored_dirs_and_binaries(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    nm = ex.workspace / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("x")
    (ex.workspace / "photo.png").write_bytes(b"\x89PNG")
    (ex.workspace / "fake.json").write_bytes(b"\x00\x01binary despite extension")
    (ex.workspace / "keep.json").write_text("{}")
    result = ex.snapshot("", 0.0)
    paths = [f["path"] for f in result["files"]]
    assert paths == ["keep.json"]


def test_snapshot_respects_host_scope_confinement(tmp_path):
    ex = LocalExec(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope")
    # Workspace scope: an absolute host path must not be readable.
    with pytest.raises(WorkspaceViolation):
        ex.snapshot(str(outside), 0.0)


def test_snapshot_host_scope_reads_granted_root(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "made-by-cli.json").write_text("{}")
    ex = LocalExec(tmp_path / "ws", host_roots=[proj])
    result = ex.snapshot(str(proj), 0.0)
    assert [f["path"] for f in result["files"]] == ["made-by-cli.json"]
    assert result["root"] == str(proj)


def test_run_result_carries_started_at(tmp_path):
    import time

    ex = LocalExec(tmp_path / "ws")
    before = time.time()
    res = ex.run("echo hi")
    assert isinstance(res.get("started_at"), float)
    assert before - 1 <= res["started_at"] <= time.time() + 1


def test_describe_advertises_scope_and_roots(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    confined = LocalExec(tmp_path / "ws1")
    assert confined.describe()["scope"] == "workspace"
    assert confined.describe()["roots"] == []

    host = LocalExec(tmp_path / "ws2", host_roots=[proj])
    d = host.describe()
    assert d["scope"] == "host"
    assert d["roots"] == [str(proj)]

    anyd = LocalExec(tmp_path / "ws3", allow_any_dir=True)
    assert anyd.describe()["roots"] == ["*"]
