"""
DeepCerebra Bridge Connector — relays a user's local Ollama / LM Studio GPU
models into the DeepCerebra web app over an authenticated WebSocket.

Flow:
  1. Dial the gateway at  <gateway>/api/bridge/agent/ws?token=<connector_token>
  2. Discover local models and send a `hello` frame.
  3. For each `infer` request, stream the local model's output back as `chunk`
     frames, then `done` (or `error`). Honors `cancel`.
  4. Re-discover + refresh `hello` periodically; auto-reconnect with backoff.

Separation of concerns: discovery + streaming live in `backends.py`; this module
only handles the relay protocol and connection lifecycle.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import websockets
from termcolor import cprint

from .backends import discover_all
from .local_exec import LocalExec, WorkspaceViolation

# ── Major config (all caps, top of module) ──────────────────────────────
RECONNECT_BASE_DELAY_SEC = 2
RECONNECT_MAX_DELAY_SEC = 30
REDISCOVER_INTERVAL_SEC = 60
WS_PING_INTERVAL_SEC = 20
WS_PING_TIMEOUT_SEC = 20


class BridgeConnector:
    def __init__(
        self,
        gateway_url: str,
        token: str,
        ollama_host: str,
        lmstudio_host: str,
        allow_exec: bool = True,
        workspace: str = "~/DeepCerebra",
        host_dirs: Optional[list] = None,
        allow_any_dir: bool = False,
    ):
        # gateway_url like wss://host  (or https://host — we normalize to ws/wss)
        self.gateway_url = gateway_url.rstrip("/")
        self.token = token
        self.ollama_host = ollama_host
        self.lmstudio_host = lmstudio_host
        self._objs: Dict[str, Any] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        # Local exec relay — the user authorizes it per device. By default every
        # command is confined to the workspace folder; host_dirs / allow_any_dir
        # opt into running in the user's real project directories (so their
        # pre-configured CLIs like Railway/AWS/OVH/WSL work with existing creds).
        self.exec: Optional[LocalExec] = (
            LocalExec(workspace, host_roots=host_dirs, allow_any_dir=allow_any_dir)
            if allow_exec
            else None
        )

    def _ws_endpoint(self) -> str:
        url = self.gateway_url
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        return f"{url}/api/bridge/agent/ws?token={self.token}"

    async def run(self) -> None:
        """Top-level loop: connect, run, reconnect with exponential backoff."""
        delay = RECONNECT_BASE_DELAY_SEC
        while True:
            try:
                await self._connect_once()
                delay = RECONNECT_BASE_DELAY_SEC  # reset after a clean session
            except (ConnectionRefusedError, OSError) as e:
                cprint(f"[connector] cannot reach gateway: {type(e).__name__}: {e}", "red")
            except Exception as e:  # noqa: BLE001
                # websockets <14 raises InvalidStatusCode(.status_code); >=14
                # raises InvalidStatus(.response.status_code). Handle both.
                status = getattr(e, "status_code", None) or getattr(
                    getattr(e, "response", None), "status_code", None
                )
                if status in (4401, 401, 403, 4403):
                    cprint(
                        f"[connector] gateway rejected the connection (HTTP {status}).\n"
                        "  Most common causes:\n"
                        "  1. Wrong --gateway host: use the API/gateway origin from the\n"
                        "     pairing card (e.g. wss://api.deepcerebra.io), not the site domain.\n"
                        "  2. Wrong deployment: deepcerebra.ai and deepcerebra.io are separate\n"
                        "     servers with separate accounts - pair on the one you use.\n"
                        "  3. Invalid or revoked token: create a new device in the web app\n"
                        "     (Local Machine page) and use the fresh token.\n"
                        "  Stopping.",
                        "red",
                    )
                    return
                if status is not None:
                    cprint(f"[connector] gateway rejected connection: {e}", "red")
                else:
                    cprint(f"[connector] session error: {type(e).__name__}: {e}", "red")

            cprint(f"[connector] reconnecting in {delay}s...", "yellow")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY_SEC)

    async def _connect_once(self) -> None:
        endpoint = self._ws_endpoint()
        safe = endpoint.split("?")[0]
        cprint(f"[connector] connecting to {safe} ...", "cyan")
        async with websockets.connect(
            endpoint,
            ping_interval=WS_PING_INTERVAL_SEC,
            ping_timeout=WS_PING_TIMEOUT_SEC,
            max_size=None,
        ) as ws:
            cprint("[connector] connected - discovering local models", "green")
            await self._send_hello(ws)

            rediscover = asyncio.create_task(self._rediscover_loop(ws))
            try:
                async for raw in ws:
                    await self._handle_frame(ws, raw)
            finally:
                rediscover.cancel()
                for task in list(self._tasks.values()):
                    task.cancel()
                self._tasks.clear()

    async def _send_hello(self, ws) -> None:
        info = await discover_all(self.ollama_host, self.lmstudio_host)
        self._objs = info.pop("_objs", {})
        n = len(info["models"])
        avail = [b["name"] for b in info["backends"] if b["available"]]
        cprint(
            f"[connector] discovered {n} model(s) across {avail or 'no'} backend(s)",
            "green" if n else "yellow",
        )
        # Local execution capability: the web app uses this both to route exec
        # requests here and to display the active scope + confined CWD/roots to
        # the user. `describe()` reports scope ("workspace"|"host"), the confined
        # workspace, and any granted host roots.
        exec_info = self.exec.describe() if self.exec else {"enabled": False}
        await ws.send(json.dumps({
            "type": "hello",
            "models": info["models"],
            "backends": info["backends"],
            "exec": exec_info,
        }))

    async def _rediscover_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(REDISCOVER_INTERVAL_SEC)
            try:
                await self._send_hello(ws)
            except Exception as e:  # noqa: BLE001
                cprint(f"[connector] rediscovery failed: {type(e).__name__}: {e}", "yellow")

    async def _handle_frame(self, ws, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except Exception:
            return
        ftype = frame.get("type")
        if ftype == "infer":
            rid = frame.get("request_id")
            self._tasks[rid] = asyncio.create_task(self._run_infer(ws, frame))
        elif ftype in ("exec", "exec_sync"):
            rid = frame.get("request_id")
            self._tasks[rid] = asyncio.create_task(self._run_exec(ws, frame))
        elif ftype == "cancel":
            rid = frame.get("request_id")
            task = self._tasks.pop(rid, None)
            if task:
                task.cancel()
        elif ftype == "revoked":
            cprint("[connector] this device was revoked from the web app. Stopping.", "red")
            await ws.close()

    async def _run_infer(self, ws, frame: Dict[str, Any]) -> None:
        rid = frame.get("request_id")
        backend_name = frame.get("backend", "ollama")
        model = frame.get("model", "")
        messages = frame.get("messages", []) or []
        options = frame.get("options", {}) or {}
        backend = self._objs.get(backend_name)

        if backend is None:
            await self._safe_send(ws, {"type": "error", "request_id": rid,
                                       "error": f"backend '{backend_name}' not available"})
            self._tasks.pop(rid, None)
            return

        cprint(f"[connector] -> infer [{backend_name}] {model} (req {rid[:8]})", "cyan")
        try:
            async for delta in backend.stream_chat(model, messages, options):
                await self._safe_send(ws, {
                    "type": "chunk",
                    "request_id": rid,
                    "content": delta.get("text", ""),
                    "kind": delta.get("kind", "content"),
                })
            await self._safe_send(ws, {"type": "done", "request_id": rid})
            cprint(f"[connector] completed req {rid[:8]}", "green")
        except asyncio.CancelledError:
            cprint(f"[connector] cancelled req {rid[:8]}", "yellow")
            raise
        except Exception as e:  # noqa: BLE001
            cprint(f"[connector] infer error req {rid[:8]}: {type(e).__name__}: {e}", "red")
            await self._safe_send(ws, {"type": "error", "request_id": rid,
                                       "error": f"{type(e).__name__}: {e}"})
        finally:
            self._tasks.pop(rid, None)

    async def _run_exec(self, ws, frame: Dict[str, Any]) -> None:
        """Fulfill an `exec` / `exec_sync` relay frame inside the confined
        workspace and reply with a single `exec_result` frame."""
        rid = frame.get("request_id")
        reply: Dict[str, Any] = {"type": "exec_result", "request_id": rid, "ok": False}
        try:
            if self.exec is None:
                reply["error"] = "local execution is disabled on this device (--no-exec)"
            elif frame.get("type") == "exec_sync":
                files = frame.get("files") or []
                cprint(f"[connector] -> sync {len(files)} file(s) into {self.exec.workspace}", "cyan")
                result = await asyncio.to_thread(self.exec.sync, files)
                reply.update(ok=True, **result)
            else:
                command = str(frame.get("command") or "")
                cwd = str(frame.get("cwd") or "")
                timeout = int(frame.get("timeout") or 0)
                cprint(f"[connector] -> exec (req {str(rid)[:8]}): {command[:120]}", "cyan")
                result = await asyncio.to_thread(self.exec.run, command, cwd, timeout)
                reply.update(ok=True, **result)
                cprint(f"[connector] exec done rc={result.get('return_code')} (req {str(rid)[:8]})",
                       "green" if result.get("return_code") == 0 else "yellow")
        except WorkspaceViolation as e:
            reply["error"] = str(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            reply["error"] = f"{type(e).__name__}: {e}"
        finally:
            self._tasks.pop(rid, None)
        await self._safe_send(ws, reply)

    @staticmethod
    async def _safe_send(ws, frame: Dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(frame))
        except Exception:
            pass
