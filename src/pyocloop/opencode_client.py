"""HTTP client for the OpenCode server REST API and SSE event stream."""

from __future__ import annotations

import json
import os
from typing import AsyncIterator, Optional

import httpx
from httpx_sse import aconnect_sse


def parse_model_string(model: str | None) -> dict | None:
    if not model:
        return None
    if "/" in model:
        provider, _, model_id = model.partition("/")
        return {"providerID": provider, "modelID": model_id}
    return {"providerID": "", "modelID": model}


class OpenCodeClient:
    def __init__(self, base_url: str, directory: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        # OpenCode uses x-opencode-directory header to scope requests to a project
        headers = {}
        if directory:
            headers["x-opencode-directory"] = directory
        self._headers = headers
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def create_session(self) -> str:
        r = await self._client.post("/session", json={})
        r.raise_for_status()
        return r.json()["id"]

    async def prompt_async(
        self,
        session_id: str,
        text: str,
        agent: Optional[str] = None,
        model_dict: Optional[dict] = None,
    ) -> None:
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if agent:
            body["agent"] = agent
        if model_dict:
            body["model"] = model_dict
        r = await self._client.post(f"/session/{session_id}/prompt_async", json=body)
        r.raise_for_status()

    async def abort_session(self, session_id: str) -> None:
        try:
            r = await self._client.post(f"/session/{session_id}/abort")
            r.raise_for_status()
        except httpx.HTTPError:
            pass

    async def get_config(self) -> dict:
        r = await self._client.get("/config")
        r.raise_for_status()
        return r.json()

    async def list_agents(self) -> list[dict]:
        r = await self._client.get("/agent")
        r.raise_for_status()
        return r.json()

    async def close(self) -> None:
        await self._client.aclose()


async def subscribe_events(
    base_url: str,
    directory: str | None = None,
) -> AsyncIterator[dict]:
    """Yield parsed SSE events as {type, properties} dicts.

    OpenCode sends events as:
        event: message
        data: {"id":"...", "type":"session.idle", "properties":{...}}

    The x-opencode-directory header is required to receive session-scoped events.
    """
    base = base_url.rstrip("/")
    if directory:
        from urllib.parse import urlencode
        url = f"{base}/event?{urlencode({'directory': directory})}"
    else:
        url = f"{base}/event"
    headers = {}

    import asyncio as _asyncio

    backoff = 0.0  # no delay on first connect

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=10.0),
        headers=headers,
    ) as client:
        while True:
            if backoff > 0:
                await _asyncio.sleep(backoff)
            try:
                async with aconnect_sse(client, "GET", url) as event_source:
                    yield {"type": "sse.connected", "properties": {"url": url}}
                    async for sse in event_source.aiter_sse():
                        if not sse.data:
                            continue
                        try:
                            data = json.loads(sse.data)
                        except json.JSONDecodeError:
                            continue
                        # Events embed type+properties in the data JSON
                        event_type = data.get("type") or sse.event or ""
                        props = data.get("properties", {})
                        if event_type and event_type != "server.connected":
                            yield {"type": event_type, "properties": props}
                # clean disconnect — wait before reconnecting
                backoff = 5.0
            except Exception as exc:
                yield {"type": "sse.error", "properties": {"error": str(exc)}}
                backoff = min((backoff or 1.0) * 2, 30.0)
