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
