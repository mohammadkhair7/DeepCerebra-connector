"""
CLI entry point for the DeepCerebra Bridge Connector.

    python -m dcc_bridge --gateway wss://your-gateway-host --token dcc_brg_xxx

Config precedence: CLI flag > environment variable > default.
  Env: DCC_BRIDGE_GATEWAY, DCC_BRIDGE_TOKEN, OLLAMA_HOST, LMSTUDIO_HOST
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from termcolor import cprint

# Windows consoles default to a legacy code page (cp1252) that can't encode
# colored/uni output; force UTF-8 so termcolor prints never crash the daemon.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from .backends import LMSTUDIO_DEFAULT_HOST, OLLAMA_DEFAULT_HOST
from .connector import BridgeConnector


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dcc_bridge",
        description="Relay your local Ollama / LM Studio GPU models into the DeepCerebra web app.",
    )
    parser.add_argument(
        "--gateway",
        default=os.getenv("DCC_BRIDGE_GATEWAY", ""),
        help="Gateway base URL (e.g. wss://app.deepcerebra.dev). Env: DCC_BRIDGE_GATEWAY",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("DCC_BRIDGE_TOKEN", ""),
        help="Connector token from the web app (Settings -> Local GPU). Env: DCC_BRIDGE_TOKEN",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.getenv("OLLAMA_HOST", OLLAMA_DEFAULT_HOST),
        help=f"Ollama base URL (default {OLLAMA_DEFAULT_HOST}). Env: OLLAMA_HOST",
    )
    parser.add_argument(
        "--lmstudio-host",
        default=os.getenv("LMSTUDIO_HOST", LMSTUDIO_DEFAULT_HOST),
        help=f"LM Studio base URL (default {LMSTUDIO_DEFAULT_HOST}). Env: LMSTUDIO_HOST",
    )
    parser.add_argument(
        "--workspace",
        default=os.getenv("DCC_BRIDGE_WORKSPACE", "~/DeepCerebra"),
        help="Folder local commands are confined to (default ~/DeepCerebra). Env: DCC_BRIDGE_WORKSPACE",
    )
    parser.add_argument(
        "--host-dir",
        action="append",
        default=None,
        metavar="PATH",
        help="Grant the web app permission to run commands in this REAL directory "
             "(repeatable). Lets pre-configured CLIs (Railway, AWS, OVH, gh, docker, "
             "WSL, …) run with your existing credentials in your own project folders. "
             "Env: DCC_BRIDGE_HOST_DIRS (os path-separator list).",
    )
    parser.add_argument(
        "--allow-any-dir",
        action="store_true",
        default=os.getenv("DCC_BRIDGE_ALLOW_ANY_DIR", "false").strip().lower() in ("1", "true", "yes"),
        help="Allow commands to run ANYWHERE on this machine (full host access). "
             "Use with care. Env: DCC_BRIDGE_ALLOW_ANY_DIR=true",
    )
    parser.add_argument(
        "--no-exec",
        action="store_true",
        default=os.getenv("DCC_BRIDGE_ALLOW_EXEC", "true").strip().lower() in ("0", "false", "no"),
        help="Disable local command execution for this device (inference relay only). "
             "Env: DCC_BRIDGE_ALLOW_EXEC=false",
    )
    return parser.parse_args()


def _resolve_host_dirs(args: argparse.Namespace) -> list[str]:
    """CLI --host-dir (repeatable) merged with DCC_BRIDGE_HOST_DIRS (path-list)."""
    dirs: list[str] = list(args.host_dir or [])
    env_val = os.getenv("DCC_BRIDGE_HOST_DIRS", "").strip()
    if env_val:
        dirs.extend([p for p in env_val.split(os.pathsep) if p.strip()])
    # De-dupe, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        key = os.path.abspath(os.path.expanduser(d))
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def main() -> int:
    args = _parse_args()

    if not args.gateway:
        cprint("[connector] --gateway (or DCC_BRIDGE_GATEWAY) is required.", "red")
        return 2
    if not args.token:
        cprint("[connector] --token (or DCC_BRIDGE_TOKEN) is required. Get one from the web app: Settings -> Local GPU.", "red")
        return 2

    allow_exec = not args.no_exec
    host_dirs = _resolve_host_dirs(args) if allow_exec else []
    allow_any_dir = bool(args.allow_any_dir) and allow_exec
    workspace_display = os.path.abspath(os.path.expanduser(args.workspace)) if allow_exec else "(disabled)"
    if allow_any_dir:
        scope_display = "host (ENTIRE MACHINE)"
    elif host_dirs:
        scope_display = "host (granted dirs)"
    elif allow_exec:
        scope_display = "workspace (confined)"
    else:
        scope_display = "(disabled)"

    cprint("=" * 64, "blue")
    cprint(" DeepCerebra Bridge Connector", "blue", attrs=["bold"])
    cprint(f"   gateway      : {args.gateway}", "blue")
    cprint(f"   ollama-host  : {args.ollama_host}", "blue")
    cprint(f"   lmstudio-host: {args.lmstudio_host}", "blue")
    cprint(f"   local exec   : {'ENABLED' if allow_exec else 'disabled'}", "blue")
    cprint(f"   exec scope   : {scope_display}", "blue")
    cprint(f"   workspace    : {workspace_display}", "blue")
    if host_dirs:
        for d in host_dirs:
            cprint(f"   host dir     : {os.path.abspath(os.path.expanduser(d))}", "blue")
    cprint("=" * 64, "blue")
    if allow_exec and not (host_dirs or allow_any_dir):
        cprint(
            "[connector] the web app may run commands on this machine, confined to the "
            "workspace above. Grant real project folders with --host-dir to use your "
            "pre-configured CLIs (Railway/AWS/OVH/WSL), or --no-exec to turn exec off.",
            "yellow",
        )
    elif allow_any_dir:
        cprint(
            "[connector] WARNING: full host access is ENABLED (--allow-any-dir). The web "
            "app may run commands anywhere on this machine. Prefer --host-dir <path> to "
            "limit this to specific project folders.",
            "red",
        )
    elif host_dirs:
        cprint(
            "[connector] host exec ENABLED for the granted directories above — the web app "
            "may run your pre-configured CLIs there with your existing credentials.",
            "yellow",
        )

    connector = BridgeConnector(
        gateway_url=args.gateway,
        token=args.token,
        ollama_host=args.ollama_host,
        lmstudio_host=args.lmstudio_host,
        allow_exec=allow_exec,
        workspace=args.workspace,
        host_dirs=host_dirs,
        allow_any_dir=allow_any_dir,
    )
    try:
        asyncio.run(connector.run())
        return 0
    except KeyboardInterrupt:
        cprint("\n[connector] stopped by user.", "yellow")
        return 0
    except Exception as e:  # noqa: BLE001
        cprint(f"[connector] fatal error: {type(e).__name__}: {e}", "red")
        return 1


if __name__ == "__main__":
    sys.exit(main())
