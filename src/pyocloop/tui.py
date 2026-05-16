"""Textual TUI for pyocloop."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import IO, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Footer, RichLog, Static

from .opencode_client import OpenCodeClient, parse_model_string, subscribe_events
from .opencode_server import OpenCodeServer
from .plan_parser import (
    PlanProgress,
    is_plan_complete,
    read_current_task,
    read_plan_complete_summary,
    read_plan_progress,
)

# ---------------------------------------------------------------------------
# Internal messages (worker → app)
# ---------------------------------------------------------------------------


class _ServerReady(Message):
    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url


class _ServerError(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class _SessionCreated(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class _SessionIdle(Message):
    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


class _SessionError(Message):
    def __init__(self, session_id: str, message: str, is_aborted: bool) -> None:
        super().__init__()
        self.session_id = session_id
        self.message = message
        self.is_aborted = is_aborted


class _FileEdited(Message):
    def __init__(self, file_path: str) -> None:
        super().__init__()
        self.file_path = file_path


class _ToolUsed(Message):
    def __init__(self, tool_name: str, detail: str = "") -> None:
        super().__init__()
        self.tool_name = tool_name
        self.detail = detail


class _TextEvent(Message):
    def __init__(self, text: str, role: str) -> None:
        super().__init__()
        self.text = text
        self.role = role


class _ReasoningEvent(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class _StepDone(Message):
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        super().__init__()
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _PlanComplete(Message):
    def __init__(self, summary: str) -> None:
        super().__init__()
        self.summary = summary


class _LoopError(Message):
    def __init__(self, message: str, recoverable: bool = True) -> None:
        super().__init__()
        self.message = message
        self.recoverable = recoverable


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

STATE_STARTING = "starting"
STATE_READY    = "ready"
STATE_RUNNING  = "running"
STATE_PAUSING  = "pausing"
STATE_PAUSED   = "paused"
STATE_COMPLETE = "complete"
STATE_ERROR    = "error"

_STATE_ICONS = {
    STATE_STARTING: "◐",
    STATE_READY:    "●",
    STATE_RUNNING:  "▶",
    STATE_PAUSING:  "◑",
    STATE_PAUSED:   "⏸",
    STATE_COMPLETE: "✓",
    STATE_ERROR:    "✗",
}

_STATE_COLORS = {
    STATE_STARTING: "yellow",
    STATE_READY:    "cyan",
    STATE_RUNNING:  "green",
    STATE_PAUSING:  "yellow",
    STATE_PAUSED:   "yellow",
    STATE_COMPLETE: "bright_green",
    STATE_ERROR:    "red",
}


# ---------------------------------------------------------------------------
# Header widget — uses Static.update() for reliable re-renders
# ---------------------------------------------------------------------------

class _Header(Static):
    DEFAULT_CSS = """
    _Header {
        height: auto;
        background: $panel;
        border-bottom: solid $primary;
        padding: 0 1;
    }
    """


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class OcloopApp(App):
    CSS = """
    Screen { layout: vertical; }
    _Header { height: auto; min-height: 3; }
    RichLog { height: 1fr; border: none; scrollbar-gutter: stable; }
    Footer  { height: 1; }
    """

    BINDINGS = [
        Binding("s",      "start_loop",    "Start",  show=True),
        Binding("space",  "toggle_pause",  "Pause",  show=True),
        Binding("r",      "retry",         "Retry",  show=True),
        Binding("q",      "request_quit",  "Quit",   show=True),
        Binding("ctrl+c", "request_quit",  "Quit",   show=False),
    ]

    def __init__(
        self,
        model: Optional[str],
        agent: Optional[str],
        prompt_file: Path,
        plan_file: Path,
        port: int,
        auto_run: bool,
        debug: bool,
        verbose: bool,
        directory: str,
        log_file: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self._model       = model
        self._agent       = agent
        self._prompt_file = prompt_file
        self._plan_file   = plan_file
        self._port        = port
        self._auto_run    = auto_run
        self._debug       = debug
        self._verbose     = verbose
        self._directory   = directory  # x-opencode-directory header value
        self._log_fh: Optional[IO[str]] = open(log_file, "a", encoding="utf-8") if log_file else None

        self._server: Optional[OpenCodeServer] = None
        self._client: Optional[OpenCodeClient] = None
        self._model_dict = parse_model_string(model)

        # App state (drives header rendering)
        self._state          = STATE_STARTING
        self._iteration      = 0
        self._plan_progress: Optional[PlanProgress] = None
        self._current_task: Optional[str] = None
        self._total_tokens   = 0

        # Session tracking
        self._current_session_id: Optional[str] = None
        self._paused         = False
        self._stop_requested = False

        # Timing
        self._iter_start_time: Optional[float] = None
        self._iter_times: list[float] = []

        # asyncio primitives (set in on_mount)
        self._idle_event:   Optional[asyncio.Event] = None
        self._start_event:  Optional[asyncio.Event] = None
        self._resume_event: Optional[asyncio.Event] = None

        # SSE dedup
        self._seen_part_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Compose & mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield _Header()
        yield RichLog(id="log", auto_scroll=True, highlight=True, markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._idle_event   = asyncio.Event()
        self._start_event  = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._refresh_header()
        self.run_worker(self._worker_server(), exclusive=False, name="server")
        self.set_interval(1.0, self._tick_elapsed)
        self.set_interval(4.0, self._reload_plan)

    # ------------------------------------------------------------------
    # Header rendering — single source of truth
    # ------------------------------------------------------------------

    def _refresh_header(self) -> None:
        self.query_one(_Header).update(self._build_header())

    def _build_header(self) -> str:
        icon  = _STATE_ICONS.get(self._state, "?")
        color = _STATE_COLORS.get(self._state, "white")
        state_str = f"[{color}]{icon} {self._state.upper()}[/{color}]"

        # Progress counter + bar
        if self._plan_progress:
            p = self._plan_progress
            automatable = p.completed + p.pending
            pct = p.percent_complete
            bar_w = 20
            filled = round(bar_w * pct / 100)
            bar = "█" * filled + "░" * (bar_w - filled)
            progress_str = f"[{p.completed}/{automatable}] [{bar}] {pct}%"
        else:
            progress_str = ""

        iter_str = f"iter:{self._iteration}" if self._iteration > 0 else ""

        # Elapsed / avg
        elapsed_str = avg_str = ""
        if self._iter_start_time and self._state in (STATE_RUNNING, STATE_PAUSING):
            e = int(time.monotonic() - self._iter_start_time)
            elapsed_str = f"{e // 60}:{e % 60:02d}"
        if self._iter_times:
            a = int(sum(self._iter_times) / len(self._iter_times))
            avg_str = f"avg:{a // 60}:{a % 60:02d}"

        model_str = f"[dim]{self._model}[/dim]" if self._model else ""
        tok_str   = f"[dim]tok:{self._total_tokens:,}[/dim]" if self._total_tokens else ""

        # Current task
        if self._current_task:
            task_str = f"[yellow]{self._current_task[:80]}[/yellow]"
        elif self._state == STATE_COMPLETE:
            task_str = "[bright_green]All tasks complete![/bright_green]"
        elif self._state == STATE_READY:
            task_str = "[dim]Press [bold]S[/bold] to start[/dim]"
        elif self._state == STATE_ERROR:
            task_str = "[dim]Press [bold]R[/bold] to retry or [bold]Q[/bold] to quit[/dim]"
        else:
            task_str = ""

        row1 = "  ".join(p for p in [state_str, progress_str, iter_str, elapsed_str, avg_str] if p)
        row2 = "  ".join(p for p in [model_str, tok_str] if p)

        lines = [row1]
        if row2:
            lines.append(row2)
        if task_str:
            lines.append(task_str)
        return "\n".join(lines)

    def _tick_elapsed(self) -> None:
        self._refresh_header()

    # ------------------------------------------------------------------
    # Plan helpers
    # ------------------------------------------------------------------

    def _reload_plan(self) -> None:
        """Re-read plan file and update header state."""
        try:
            self._plan_progress = read_plan_progress(self._plan_file)
            self._current_task  = read_current_task(self._plan_file)
        except Exception:
            pass
        # If SSE session.idle never arrived but plan is done, unblock the loop
        if (
            self._idle_event
            and not self._idle_event.is_set()
            and self._state == STATE_RUNNING
            and self._current_session_id is not None
        ):
            try:
                if is_plan_complete(self._plan_file):
                    self._idle_event.set()
            except Exception:
                pass
        self._refresh_header()

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    def _log(self, kind: str, text: str, detail: str = "") -> None:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]  # millisecond precision
        if self._log_fh:
            d = f" | {detail}" if detail else ""
            self._log_fh.write(f"{ts} [{kind}] {text}{d}\n")
            self._log_fh.flush()
        log = self.query_one(RichLog)
        colors = {
            "start": "cyan", "idle": "cyan", "task": "yellow", "edit": "green",
            "error": "red",  "tool": "magenta", "read": "blue",  "ai": "white",
            "think": "dim",  "info": "dim",     "complete": "bright_green",
        }
        c  = colors.get(kind, "white")
        br = f"[{c}][{kind}][/{c}]"
        ds = f" [dim]{detail[:80]}[/dim]" if detail else ""
        log.write(f"[dim]{ts}[/dim] {br} {text}{ds}")

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def _worker_server(self) -> None:
        server = OpenCodeServer()
        self._server = server
        try:
            url = await server.start(port=self._port, timeout=30.0)
            self.post_message(_ServerReady(url))
        except Exception as exc:
            self.post_message(_ServerError(str(exc)))

    async def _worker_sse(self, url: str) -> None:
        async for event in subscribe_events(url, directory=self._directory):
            etype = event.get("type", "")
            props = event.get("properties", {})
            if etype == "sse.connected":
                if self._verbose:
                    self._log("info", "SSE connected", props.get("url", ""))
                continue
            if etype == "sse.error":
                self._log("error", "SSE error", props.get("error", ""))
                continue
            if self._verbose:
                self._log("info", f"SSE {etype}", str(props)[:80])
            msg = self._dispatch_sse(etype, props)
            if msg is not None:
                self.post_message(msg)

    async def _worker_loop(self) -> None:
        assert self._idle_event and self._start_event and self._resume_event
        assert self._client

        if self._auto_run:
            self._start_event.set()

        await self._start_event.wait()
        self._start_event.clear()

        self._state = STATE_RUNNING
        self._refresh_header()
        self._log("start", "Loop started")

        while not self._stop_requested:
            # Refresh plan & check completion BEFORE creating a session
            self._reload_plan()

            if is_plan_complete(self._plan_file):
                summary = read_plan_complete_summary(self._plan_file) or "Done."
                self.post_message(_PlanComplete(summary))
                return

            # Create session
            try:
                session_id = await self._client.create_session()
            except Exception as exc:
                self.post_message(_LoopError(f"Failed to create session: {exc}"))
                return

            self._current_session_id = session_id
            self._iteration += 1
            self._iter_start_time = time.monotonic()
            self._idle_event.clear()
            self._log("start", f"Session {session_id[:8]} (iter {self._iteration})")
            self._refresh_header()

            # Build prompt (replace placeholder with absolute plan path)
            try:
                prompt_text = self._prompt_file.read_text(encoding="utf-8")
                prompt_text = prompt_text.replace("{{PLAN_FILE}}", str(self._plan_file))
            except Exception as exc:
                self.post_message(_LoopError(f"Failed to read prompt: {exc}"))
                return

            # Send prompt
            try:
                await self._client.prompt_async(
                    session_id,
                    prompt_text,
                    agent=self._agent,
                    model_dict=self._model_dict,
                )
            except Exception as exc:
                self.post_message(_LoopError(f"Failed to send prompt: {exc}"))
                return

            # Wait for session idle
            await self._idle_event.wait()

            if self._iter_start_time:
                self._iter_times.append(time.monotonic() - self._iter_start_time)
            self._iter_start_time = None
            self._current_session_id = None

            if self._stop_requested:
                break

            # Handle pause
            if self._paused:
                self._state = STATE_PAUSED
                self._refresh_header()
                self._log("info", "Paused — press Space to resume")
                self._resume_event.clear()
                await self._resume_event.wait()
                if self._stop_requested:
                    break
                self._state = STATE_RUNNING
                self._refresh_header()
                self._log("info", "Resumed")

        self._log("info", "Loop stopped")

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def on__server_ready(self, msg: _ServerReady) -> None:
        self._client = OpenCodeClient(msg.url, directory=self._directory)
        self._state  = STATE_READY
        self._log("start", f"OpenCode server ready at {msg.url}")
        self._refresh_header()

        if not self._model:
            self.run_worker(self._fetch_model(), exclusive=False, name="fetch-model")

        self.run_worker(self._worker_sse(msg.url), exclusive=False, name="sse")
        self.run_worker(self._worker_loop(),       exclusive=False, name="loop")

    async def _fetch_model(self) -> None:
        assert self._client
        try:
            cfg = await self._client.get_config()
            model = cfg.get("model") or cfg.get("modelID") or ""
            if isinstance(model, dict):
                # model might be {"providerID": "...", "modelID": "..."}
                model = f"{model.get('providerID','')}/{model.get('modelID','')}".strip("/")
            if model:
                self._model = str(model)
                self._refresh_header()
        except Exception:
            pass

    def on__server_error(self, msg: _ServerError) -> None:
        self._state = STATE_ERROR
        self._log("error", f"Server failed to start: {msg.error}")
        self._refresh_header()

    def on__session_created(self, msg: _SessionCreated) -> None:
        # Reset per-session state
        self._seen_part_ids.clear()
        self._total_tokens = 0
        self._refresh_header()

    def on__session_idle(self, msg: _SessionIdle) -> None:
        # Accept if session ID matches, or if it's empty (property key unknown)
        if not msg.session_id or msg.session_id == self._current_session_id:
            self._log("idle", "Session idle")
            assert self._idle_event
            self._idle_event.set()

    def on__session_error(self, msg: _SessionError) -> None:
        if msg.session_id and msg.session_id != self._current_session_id:
            return
        if msg.is_aborted:
            self._log("info", "Session aborted")
            assert self._idle_event
            self._idle_event.set()
        else:
            self._log("error", f"Session error: {msg.message}")
            self._state = STATE_ERROR
            self._refresh_header()
            assert self._idle_event
            self._idle_event.set()

    def on__file_edited(self, msg: _FileEdited) -> None:
        edited = Path(msg.file_path).resolve()
        if edited == self._plan_file:
            self._reload_plan()
        else:
            self._log("edit", edited.name)

    def on__tool_used(self, msg: _ToolUsed) -> None:
        self._log("tool", msg.tool_name, msg.detail)

    def on__text_event(self, msg: _TextEvent) -> None:
        if msg.role == "assistant":
            self._log("ai", msg.text[:60].replace("\n", " "))

    def on__reasoning_event(self, msg: _ReasoningEvent) -> None:
        self._log("think", f"[dim]{msg.text[:60].replace(chr(10), ' ')}[/dim]")

    def on__step_done(self, msg: _StepDone) -> None:
        self._total_tokens += msg.input_tokens + msg.output_tokens
        self._refresh_header()

    def on__plan_complete(self, msg: _PlanComplete) -> None:
        self._state        = STATE_COMPLETE
        self._current_task = None
        self._reload_plan()   # update final counts
        self._refresh_header()
        self._log("complete", "Plan complete!")
        log = self.query_one(RichLog)
        log.write("")
        log.write("[bright_green bold]╔══════════════════════════════════════╗[/bright_green bold]")
        log.write("[bright_green bold]║           PLAN COMPLETE              ║[/bright_green bold]")
        log.write("[bright_green bold]╚══════════════════════════════════════╝[/bright_green bold]")
        log.write("")
        for line in msg.summary.splitlines():
            log.write(f"  {line}")
        log.write("")
        log.write("[dim]Press Q to quit.[/dim]")

    def on__loop_error(self, msg: _LoopError) -> None:
        self._state = STATE_ERROR
        self._refresh_header()
        self._log("error", msg.message)
        if msg.recoverable:
            self.query_one(RichLog).write(
                "[dim]Press [bold]R[/bold] to retry or [bold]Q[/bold] to quit.[/dim]"
            )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_start_loop(self) -> None:
        if self._state == STATE_READY:
            assert self._start_event
            self._start_event.set()

    def action_toggle_pause(self) -> None:
        if self._state == STATE_RUNNING:
            self._paused = True
            self._state  = STATE_PAUSING
            self._refresh_header()
        elif self._state in (STATE_PAUSED, STATE_PAUSING):
            self._paused = False
            self._state  = STATE_RUNNING
            assert self._resume_event
            self._resume_event.set()
            self._refresh_header()

    def action_retry(self) -> None:
        if self._state == STATE_ERROR:
            self._state = STATE_READY
            self._refresh_header()
            self._log("info", "Ready — press S to start")

    def action_request_quit(self) -> None:
        self._stop_requested = True
        if self._idle_event:
            self._idle_event.set()
        if self._resume_event:
            self._resume_event.set()
        if self._client and self._current_session_id:
            self.run_worker(self._abort_and_quit(), exclusive=False, name="abort-quit")
        else:
            self._do_quit()

    async def _abort_and_quit(self) -> None:
        if self._client and self._current_session_id:
            await self._client.abort_session(self._current_session_id)
        self._do_quit()

    def _do_quit(self) -> None:
        if self._server:
            self._server.close()
        if self._log_fh:
            self._log_fh.close()
        self.exit()

    # ------------------------------------------------------------------
    # SSE event dispatcher
    # ------------------------------------------------------------------

    def _dispatch_sse(self, etype: str, props: dict) -> Optional[Message]:
        if etype == "session.idle":
            sid = props.get("sessionID") or props.get("id") or props.get("session_id") or ""
            return _SessionIdle(sid)

        if etype == "session.created":
            info = props.get("info") or props
            sid  = info.get("id", "")
            return _SessionCreated(sid) if sid else None

        if etype == "session.error":
            sid     = props.get("sessionID", "")
            raw_err = props.get("error", {})
            if isinstance(raw_err, dict):
                name       = raw_err.get("name", "")
                message    = (
                    raw_err.get("message")
                    or (raw_err.get("data") or {}).get("message", "Unknown error")
                )
                is_aborted = name == "MessageAbortedError"
            else:
                message    = str(raw_err) if raw_err else "Unknown error"
                is_aborted = False
            return _SessionError(sid, message, is_aborted)

        if etype == "file.edited":
            return _FileEdited(props.get("file", ""))

        if etype == "message.part.updated":
            part = props.get("part", {})
            if not part or not part.get("id"):
                return None
            part_id = part["id"]
            ptype   = part.get("type", "")

            if ptype in ("tool-use", "tool"):
                status = (part.get("state") or {}).get("status", "")
                if status == "running" and part_id not in self._seen_part_ids:
                    self._seen_part_ids.add(part_id)
                    tool = (
                        (part.get("state") or {}).get("tool")
                        or part.get("tool")
                        or "unknown"
                    )
                    inp = str((part.get("state") or {}).get("input", ""))[:60]
                    return _ToolUsed(tool, inp)

            elif ptype == "text":
                if part_id not in self._seen_part_ids:
                    self._seen_part_ids.add(part_id)
                    return _TextEvent(part.get("text", ""), "assistant")

            elif ptype == "reasoning":
                if part_id not in self._seen_part_ids:
                    self._seen_part_ids.add(part_id)
                    return _ReasoningEvent(part.get("text", ""))

            elif ptype == "step-finish":
                if part_id not in self._seen_part_ids:
                    self._seen_part_ids.add(part_id)
                    tokens = part.get("tokens", {})
                    return _StepDone(tokens.get("input", 0), tokens.get("output", 0))

        return None
