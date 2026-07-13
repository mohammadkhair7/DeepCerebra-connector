"""
Local execution for the DeepCerebra Bridge Connector.

Lets the web app run real commands on THIS machine. Two scopes, chosen by the
device owner when starting the connector:

* **workspace** (default, safest) — every command runs inside a single dedicated
  folder (default ``~/DeepCerebra``) and file syncs / working directories that
  try to escape it are refused. Good for building/testing the browser project.

* **host** (opt-in) — the web app may run commands in the user's REAL project
  directories so pre-configured CLI tools work with their existing credentials
  and state: Railway, AWS CLI, OVH CLI, gh, docker, kubectl, WSL, etc. The owner
  grants one or more allowed root directories with ``--host-dir`` (repeatable),
  or the whole machine with ``--allow-any-dir``. A command's working directory
  must fall inside a granted root; VFS file syncs still land only in the confined
  workspace, never in the host roots.

Trust model: the user explicitly paired this device, left execution enabled
(``--no-exec`` turns it off entirely), and — for host scope — explicitly listed
the directories the web app may touch. Nothing is exposed by default beyond the
confined workspace. The active scope + granted roots are printed at startup and
shown in the web app so the user always knows exactly where commands run.

Cross-platform: plain ``subprocess`` with ``shell=True`` (cmd.exe on Windows,
/bin/sh on Linux/macOS), inheriting the user's environment and PATH so their
installed CLIs and credentials resolve exactly as in their own terminal. Mobile
devices don't run the connector — the web app on a phone/tablet relays to
whichever paired computer is online.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Mirror the server sandbox's sync caps so a malicious/buggy page can't fill
# the disk through the relay.
MAX_FILES = 4000
MAX_TOTAL_BYTES = 32 * 1024 * 1024   # 32 MB per sync
MAX_FILE_BYTES = 2 * 1024 * 1024     # 2 MB per file
MAX_OUTPUT_BYTES = 1 * 1024 * 1024   # 1 MB stdout / stderr each
DEFAULT_TIMEOUT_SEC = 300
# Builds (docker compose build, npm ci, …) legitimately run for many minutes;
# 30 min cap. The gateway's relay wait (DCC_BRIDGE_EXEC_TIMEOUT_SEC) is higher.
MAX_TIMEOUT_SEC = 1800

# ── Reverse-sync (snapshot) tuning ───────────────────────────────────────────
# After a command runs on this machine it may CREATE files (e.g. `railway link`
# writing railway.json, a scaffolder emitting sources). Those live only on disk
# here; the web app's Project Explorer reads the browser VFS, so without pulling
# them back the file "vanishes" from the tree. snapshot() returns files under
# the command's working directory that changed since it started, bounded like a
# sync and skipping heavy/generated trees the explorer never shows anyway.
SNAPSHOT_IGNORED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", "dist", "build", ".cache", ".turbo",
    ".gradle", "target", ".idea", ".DS_Store", ".terraform", "vendor",
    ".svelte-kit", "coverage", ".parcel-cache", "bin", "obj",
}
# mtime granularity + small clock jitter: include files touched slightly before
# the recorded start so a fast command's output is never missed.
SNAPSHOT_MTIME_EPSILON_SEC = 2.0
# Extensions we treat as textual (VFS is text-only). Anything else is skipped;
# a NUL-byte sniff is the final guard for mislabeled files.
SNAPSHOT_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".json", ".jsonc", ".json5", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue",
    ".svelte", ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".kts",
    ".go", ".rs", ".rb", ".php", ".swift", ".m", ".mm", ".scala", ".clj",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql", ".proto", ".xml", ".svg", ".csv", ".tsv",
    ".gitignore", ".dockerignore", ".editorconfig", ".gitattributes",
    "dockerfile", "makefile", "procfile", ".lock", ".r", ".jl", ".dart",
    ".tf", ".tfvars", ".hcl", ".gradle", ".ipynb", ".tex",
}


def _looks_textual(name: str) -> bool:
    low = name.lower()
    if low in SNAPSHOT_TEXT_EXT:  # extensionless conventions (Dockerfile, Makefile)
        return True
    ext = os.path.splitext(low)[1]
    return ext in SNAPSHOT_TEXT_EXT


class WorkspaceViolation(Exception):
    """A path tried to escape the allowed execution area."""


class LocalExec:
    """Confined file-sync + command execution.

    File syncs are ALWAYS rooted at ``workspace``. Command working directories
    are confined to ``workspace`` too, unless host scope is enabled: then a cwd
    may be any absolute directory inside one of ``host_roots`` (or anywhere when
    ``allow_any_dir`` is set).
    """

    def __init__(
        self,
        workspace: str | Path,
        host_roots: Optional[List[str | Path]] = None,
        allow_any_dir: bool = False,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allow_any_dir = bool(allow_any_dir)
        # Normalize granted host roots to resolved, existing directories.
        self.host_roots: List[Path] = []
        for r in host_roots or []:
            try:
                p = Path(r).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue
            if p.is_dir():
                self.host_roots.append(p)

    # ── scope introspection (advertised to the web app) ──────────────────────
    @property
    def host_enabled(self) -> bool:
        return self.allow_any_dir or bool(self.host_roots)

    @property
    def scope(self) -> str:
        return "host" if self.host_enabled else "workspace"

    def describe(self) -> Dict[str, Any]:
        """Capability advertisement embedded in the connector's ``hello`` frame."""
        roots = ["*"] if self.allow_any_dir else [str(p) for p in self.host_roots]
        return {
            "enabled": True,
            "scope": self.scope,
            "workspace": str(self.workspace),
            # Real host directories the web app may set as a working directory.
            # "*" means the whole machine (allow-any-dir).
            "roots": roots,
        }

    # ── confinement ──────────────────────────────────────────────────────
    def safe_path(self, rel: str) -> Path:
        """Resolve a browser-supplied relative path strictly inside the
        workspace; raises WorkspaceViolation on traversal attempts. Used for VFS
        file sync, which never escapes the confined workspace regardless of
        scope."""
        rel = (rel or "").replace("\\", "/").strip().lstrip("/")
        candidate = (self.workspace / rel).resolve() if rel else self.workspace
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            raise WorkspaceViolation(f"path escapes the workspace: {rel!r}")
        return candidate

    def _within_host_roots(self, path: Path) -> bool:
        if self.allow_any_dir:
            return True
        for root in self.host_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def resolve_cwd(self, cwd: str) -> Path:
        """Resolve the requested working directory for a command.

        * workspace scope: identical to the historical behavior — a relative
          path inside the confined workspace (traversal refused), falling back
          to the workspace root when the folder doesn't exist.
        * host scope: an ABSOLUTE path is accepted when it lies inside a granted
          host root (or anywhere with allow-any-dir); relative paths are still
          resolved against the workspace. This is what lets the web app run a
          pre-configured CLI in the user's real project directory.
        """
        raw = (cwd or "").strip()
        if raw and self.host_enabled:
            expanded = os.path.expanduser(raw)
            # Treat as a host path when it's absolute (or ~-based).
            if os.path.isabs(expanded):
                candidate = Path(expanded).resolve()
                if not self._within_host_roots(candidate):
                    raise WorkspaceViolation(
                        f"working directory is outside the granted host roots: {raw!r}"
                    )
                if not candidate.is_dir():
                    raise WorkspaceViolation(f"working directory does not exist: {raw!r}")
                return candidate
        # Relative path (any scope) → confined to the workspace.
        workdir = self.safe_path(raw)
        if not workdir.is_dir():
            workdir = self.workspace
        return workdir

    # ── VFS sync ─────────────────────────────────────────────────────────
    def sync(self, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Materialize the browser project into the workspace (bounded)."""
        written = 0
        total = 0
        for f in files[:MAX_FILES]:
            rel = str(f.get("path") or "")
            content = f.get("content")
            if not rel or not isinstance(content, str):
                continue
            data = content.encode("utf-8", "replace")
            if len(data) > MAX_FILE_BYTES:
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                break
            target = self.safe_path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            written += 1
        return {"written": written, "workspace": str(self.workspace)}

    # ── command execution ────────────────────────────────────────────────
    def run(
        self,
        command: str,
        cwd: str = "",
        timeout: int = DEFAULT_TIMEOUT_SEC,
        on_spawn: Optional[Callable[[subprocess.Popen], None]] = None,
    ) -> Dict[str, Any]:
        """Run one shell command. cwd is confined per the active scope.

        The relay is NON-INTERACTIVE: stdin is /dev/null, so a command that
        prompts for input (interactive menus, confirmations) errors or ends
        quickly instead of hanging until the timeout — callers should use
        non-interactive flags. ``on_spawn`` receives the Popen handle so the
        connector can kill the whole process tree on user cancellation
        (Ctrl+C in the web terminal).
        """
        workdir = self.resolve_cwd(cwd)
        # Recorded on THIS machine's clock so a follow-up snapshot() can find the
        # files the command created without any cross-machine clock skew.
        started_at = time.time()
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_SEC), MAX_TIMEOUT_SEC))
        popen_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            # NEW_PROCESS_GROUP: lets a cancel signal the whole tree.
            # CREATE_NO_WINDOW: detach from the connector's own console so an
            # interactive TUI (e.g. bare `railway link`) can't read our console
            # and hang forever — with no console it errors out immediately.
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            # Own session (no controlling TTY) so a command that tries to read
            # /dev/tty fails fast instead of blocking, and a cancel can signal
            # the whole process group.
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        if on_spawn is not None:
            try:
                on_spawn(proc)
            except Exception:  # noqa: BLE001 — registration must never break the run
                pass
        try:
            out, err = proc.communicate(timeout=timeout)
            return {
                "return_code": proc.returncode,
                "stdout": _cap(out),
                "stderr": _cap(err),
                "cwd": str(workdir),
                "started_at": started_at,
            }
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out, err = b"", b""
            return {
                "return_code": 124,
                "stdout": _cap(out),
                "stderr": _cap(err) + (
                    f"\ncommand timed out after {timeout}s. Note: this relay is "
                    "non-interactive — if the command was waiting for input "
                    "(menu/confirmation), rerun it with non-interactive flags."
                ).lstrip("\n"),
                "cwd": str(workdir),
                "started_at": started_at,
            }

    # ── reverse sync (snapshot files a command created/changed) ─────────────
    def snapshot(self, cwd: str, since: float) -> Dict[str, Any]:
        """Return files under the command's working directory that changed at or
        after ``since`` (epoch seconds on THIS machine), so the web app can pull
        command-created files (e.g. railway.json) back into the browser VFS and
        show them in the Project Explorer.

        Confinement mirrors execution: the root is resolved exactly like a
        command cwd (workspace-relative, or an absolute host dir inside a granted
        root), so a page can never read outside what the owner authorized. Heavy
        / generated trees and binaries are skipped, and the payload is bounded.
        """
        base = self.resolve_cwd(cwd)
        cutoff = float(since) - SNAPSHOT_MTIME_EPSILON_SEC
        files: List[Dict[str, Any]] = []
        total = 0
        scanned = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune heavy/generated dirs in place so os.walk never descends them.
            dirnames[:] = [d for d in dirnames if d not in SNAPSHOT_IGNORED_DIRS]
            for fname in filenames:
                scanned += 1
                if scanned > 200_000:  # pathological tree guard
                    truncated = True
                    break
                if not _looks_textual(fname):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                try:
                    raw = fpath.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:  # binary despite a textual extension
                    continue
                total += len(raw)
                if total > MAX_TOTAL_BYTES:
                    truncated = True
                    break
                try:
                    rel = fpath.relative_to(base).as_posix()
                except ValueError:
                    continue
                files.append({"path": rel, "content": raw.decode("utf-8", "replace")})
                if len(files) >= MAX_FILES:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "files": files,
            "root": str(base),
            "count": len(files),
            "truncated": truncated,
        }


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a relayed command AND everything it spawned (with shell=True
    the real work happens in child processes). Used for user cancellation
    (web-terminal Ctrl+C) and for timeouts."""
    try:
        if os.name == "nt":
            # taskkill /T walks the child tree; /F forces. More reliable than
            # CTRL_BREAK for arbitrary console-less shells.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _cap(raw: bytes | None) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n…[output truncated]"
    return text
