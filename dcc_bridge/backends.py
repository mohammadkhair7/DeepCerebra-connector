"""
Local inference backends for the DeepCerebra connector.

Discovers and streams from the user's local LLM servers:
  - Ollama       (OpenAI-compatible at http://localhost:11434/v1)
  - LM Studio    (OpenAI-compatible at http://localhost:1234/v1)

Discovery uses each server's native listing endpoint; chat streaming uses the
OpenAI-compatible surface via ``AsyncOpenAI`` so tool-less completions stream
identically across both backends.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from openai import AsyncOpenAI
from termcolor import cprint

# ── Major config (all caps, top of module) ──────────────────────────────
OLLAMA_DEFAULT_HOST = "http://localhost:11434"
LMSTUDIO_DEFAULT_HOST = "http://localhost:1234"
DISCOVERY_TIMEOUT_SEC = 8
# Local servers ignore the key, but the OpenAI client requires a non-empty one.
LOCAL_API_KEY = "dcc-local"


class LocalBackend:
    """Common interface over a local OpenAI-compatible inference server."""

    name: str = "base"

    def __init__(self, host: str):
        self.host = host.rstrip("/")

    @property
    def base_url(self) -> str:
        return f"{self.host}/v1"

    async def discover(self) -> List[Dict[str, Any]]:  # pragma: no cover - overridden
        raise NotImplementedError

    async def is_available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.host}/v1/models")
                return resp.status_code == 200
        except Exception:
            return False

    async def stream_chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        options: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, str], None]:
        """Stream typed deltas from the local server (OpenAI-compatible).

        Yields ``{"kind": "content"|"reasoning", "text": str}``. "Thinking" models
        (qwen3, deepseek-r1, …) stream their chain-of-thought in `reasoning` /
        `reasoning_content` rather than `content`; we surface both so the chat
        bubble isn't empty while the model reasons (the web app renders reasoning
        in a separate collapsible section, exactly like cloud reasoning models).
        """
        options = options or {}
        client = AsyncOpenAI(base_url=self.base_url, api_key=LOCAL_API_KEY)
        kwargs: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if options.get("temperature") is not None:
            kwargs["temperature"] = options["temperature"]
        if options.get("max_tokens") is not None:
            kwargs["max_tokens"] = options["max_tokens"]

        stream = await client.chat.completions.create(**kwargs)
        async for event in stream:
            try:
                delta = event.choices[0].delta
            except (AttributeError, IndexError):
                continue
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                yield {"kind": "reasoning", "text": reasoning}
            content = getattr(delta, "content", None)
            if content:
                yield {"kind": "content", "text": content}


class OllamaBackend(LocalBackend):
    name = "ollama"

    async def discover(self) -> List[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SEC) as client:
                resp = await client.get(f"{self.host}/api/tags")
                resp.raise_for_status()
                data = resp.json()
            out = []
            for m in data.get("models", []) or []:
                details = m.get("details", {}) or {}
                out.append({
                    "id": m.get("name", ""),
                    "backend": "ollama",
                    "family": details.get("family", ""),
                    "parameter_size": details.get("parameter_size", ""),
                    "size": m.get("size", 0),
                })
            return [m for m in out if m["id"]]
        except Exception as e:
            cprint(f"[ollama] discovery failed: {type(e).__name__}: {e}", "yellow")
            return []


class LMStudioBackend(LocalBackend):
    name = "lmstudio"

    async def discover(self) -> List[Dict[str, Any]]:
        # Prefer the richer native endpoint; fall back to /v1/models.
        try:
            async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SEC) as client:
                resp = await client.get(f"{self.host}/api/v0/models")
                if resp.status_code == 200:
                    data = resp.json()
                    out = []
                    for m in data.get("data", []) or []:
                        out.append({
                            "id": m.get("id", ""),
                            "backend": "lmstudio",
                            "family": m.get("arch", "") or m.get("type", ""),
                            "quantization": m.get("quantization", ""),
                            "state": m.get("state", ""),
                        })
                    out = [m for m in out if m["id"]]
                    if out:
                        return out
        except Exception as e:
            cprint(f"[lmstudio] native discovery unavailable: {type(e).__name__}: {e}", "yellow")

        try:
            async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT_SEC) as client:
                resp = await client.get(f"{self.host}/v1/models")
                resp.raise_for_status()
                data = resp.json()
            return [
                {"id": m.get("id", ""), "backend": "lmstudio"}
                for m in data.get("data", []) or []
                if m.get("id")
            ]
        except Exception as e:
            cprint(f"[lmstudio] discovery failed: {type(e).__name__}: {e}", "yellow")
            return []


async def discover_all(ollama_host: str, lmstudio_host: str) -> Dict[str, Any]:
    """Discover both backends concurrently; return models + backend availability."""
    backends = {
        "ollama": OllamaBackend(ollama_host),
        "lmstudio": LMStudioBackend(lmstudio_host),
    }
    results = await asyncio.gather(
        *[b.discover() for b in backends.values()], return_exceptions=True
    )

    models: List[Dict[str, Any]] = []
    backend_status: List[Dict[str, Any]] = []
    for (name, backend), res in zip(backends.items(), results):
        model_list = res if isinstance(res, list) else []
        models.extend(model_list)
        backend_status.append({
            "name": name,
            "host": backend.host,
            "available": len(model_list) > 0,
            "model_count": len(model_list),
        })
    return {"models": models, "backends": backend_status, "_objs": backends}
