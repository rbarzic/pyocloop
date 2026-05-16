"""Start and manage the OpenCode server subprocess."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from typing import Optional


class OpenCodeServer:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self.url: Optional[str] = None

    async def start(
        self,
        hostname: str = "127.0.0.1",
        port: int = 4096,
        timeout: float = 30.0,
        config: Optional[dict] = None,
    ) -> str:
        args = ["opencode", "serve", f"--hostname={hostname}", f"--port={port}"]
        env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(config or {})}

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,  # own process group so all children die on close
        )

        try:
            url = await asyncio.wait_for(self._wait_for_ready(), timeout=timeout)
        except asyncio.TimeoutError:
            self.close()
            raise TimeoutError(f"OpenCode server did not start within {timeout}s")

        self.url = url
        return url

    async def _wait_for_ready(self) -> str:
        assert self._proc and self._proc.stdout
        async for raw in self._proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if "opencode server listening" in line:
                m = re.search(r"on\s+(https?://\S+)", line)
                if m:
                    return m.group(1)
        raise RuntimeError("OpenCode server exited before becoming ready")

    def close(self) -> None:
        if self._proc is not None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            self._proc = None
        self.url = None

    async def __aenter__(self) -> "OpenCodeServer":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.close()
