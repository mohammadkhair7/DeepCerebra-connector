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
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

# Mirror the server sandbox's sync caps so a malicious/buggy page can't fill
# the disk through the relay.
MAX_FILES = 4000
MAX_TOTAL_BYTES = 32 * 1024 * 1024   # 32 MB per sync
MAX_FILE_BYTES = 2 * 1024 * 1024     # 2 MB per file
MAX_OUTPUT_BYTES = 1 * 1024 * 1024   # 1 MB stdout / stderr each
DEFAULT_TIMEOUT_SEC = 300
MAX_TIMEOUT_SEC = 900


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
    def run(self, command: str, cwd: str = "", timeout: int = DEFAULT_TIMEOUT_SEC) -> Dict[str, Any]:
        """Run one shell command. cwd is confined per the active scope."""
        workdir = self.resolve_cwd(cwd)
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_SEC), MAX_TIMEOUT_SEC))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                timeout=timeout,
            )
            return {
                "return_code": proc.returncode,
                "stdout": _cap(proc.stdout),
                "stderr": _cap(proc.stderr),
                "cwd": str(workdir),
            }
        except subprocess.TimeoutExpired:
            return {
                "return_code": 124,
                "stdout": "",
                "stderr": f"command timed out after {timeout}s",
                "cwd": str(workdir),
            }


def _cap(raw: bytes | None) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace")
    if len(text) > MAX_OUTPUT_BYTES:
        return text[:MAX_OUTPUT_BYTES] + "\n…[output truncated]"
    return text
