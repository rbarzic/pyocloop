"""CLI entry point for pyocloop."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="ocloop",
    help="OCLoop — orchestrate OpenCode to execute tasks from a PLAN.md file iteratively.",
    add_completion=False,
    no_args_is_help=True,
)

_PLAN_TEMPLATE = """\
# Project Plan

## Overview

Describe the goal of this project here.

## Backlog

### Phase 1

- [ ] First task description
- [ ] Second task description
- [ ] Third task description
"""

_PROMPT_TEMPLATE = """\
---
description: Execute loop
---

Execute the next task from {{PLAN_FILE}}.

Before starting:
1. Read {{PLAN_FILE}} fully

Task selection (CRITICAL):
- Work through phases IN ORDER — complete Phase N before starting Phase N+1
- Pick the FIRST uncompleted task in the earliest incomplete phase
- Skip [MANUAL] and [BLOCKED] items
- NEVER batch tasks across different phases

Execute:
1. Apply the requested changes

After completion:
1. Update {{PLAN_FILE}} marking completed items with [x]

2. If you cannot complete a task (permissions, external service, needs human input):
   - Add [BLOCKED: reason] to that task line in {{PLAN_FILE}}
   - Continue with other tasks

Completion check:
- If all non-[MANUAL] tasks are either [x] or [BLOCKED]:
  - Append `<plan-complete>SUMMARY_OF_WORK_DONE_AND_REMAINING_MANUAL_TASKS</plan-complete>` to the end of {{PLAN_FILE}}
  - Exit the session
- Do NOT skip automatable tasks — if a task seems hard but doable, attempt it
"""


@app.command()
def run(
    model: Optional[str] = typer.Option(
        None, "-m", "--model", help="Model to use (format: providerID/modelID). List available models with: opencode models"
    ),
    agent: Optional[str] = typer.Option(
        None, "-a", "--agent", help="Agent to use"
    ),
    prompt: Path = typer.Option(
        ".loop-prompt.md", "--prompt", help="Path to loop prompt file"
    ),
    plan: Path = typer.Option(
        "PLAN.md", "--plan", help="Path to plan file"
    ),
    port: int = typer.Option(
        4096, "-p", "--port", help="OpenCode server port"
    ),
    run_now: bool = typer.Option(
        False, "-r", "--run", help="Start iterations immediately"
    ),
    debug: bool = typer.Option(
        False, "-d", "--debug", help="Debug mode (skip plan file validation)"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Log every SSE event in the activity panel"
    ),
    log: Optional[Path] = typer.Option(
        None, "--log", help="Write all log entries to this file"
    ),
) -> None:
    """Run the OCLoop orchestration loop."""
    # Capture cwd before resolve() — used as x-opencode-directory to scope SSE events
    directory = os.getcwd()

    # Path bug fix: resolve all paths to absolute immediately
    prompt_abs = prompt.resolve()
    plan_abs = plan.resolve()

    if not debug:
        if not plan_abs.exists():
            typer.echo(f"Error: Plan file not found: {plan_abs}", err=True)
            typer.echo(
                f"\nTip: run  ocloop bootstrap .  to create starter files, or use --debug to skip validation.\n",
                err=True,
            )
            raise typer.Exit(1)

        if not prompt_abs.exists():
            typer.echo(f"Error: Prompt file not found: {prompt_abs}", err=True)
            typer.echo(
                f"\nTip: run  ocloop bootstrap .  to create starter files.\n",
                err=True,
            )
            raise typer.Exit(1)

    from .tui import OcloopApp

    OcloopApp(
        model=model,
        agent=agent,
        prompt_file=prompt_abs,
        plan_file=plan_abs,
        port=port,
        auto_run=run_now,
        debug=debug,
        verbose=verbose,
        directory=directory,
        log_file=log,
    ).run()


@app.command()
def bootstrap(
    directory: Path = typer.Argument(
        Path("."), help="Directory to initialise (default: current directory)"
    ),
    force: bool = typer.Option(
        False, "-f", "--force", help="Overwrite existing files"
    ),
) -> None:
    """Create a starter PLAN.md and .loop-prompt.md in DIRECTORY."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)

    plan_file   = directory / "PLAN.md"
    prompt_file = directory / ".loop-prompt.md"

    created = []
    skipped = []

    for path, content in [(plan_file, _PLAN_TEMPLATE), (prompt_file, _PROMPT_TEMPLATE)]:
        if path.exists() and not force:
            skipped.append(path.name)
        else:
            path.write_text(content, encoding="utf-8")
            created.append(path.name)

    for name in created:
        typer.echo(f"  created  {directory / name}")
    for name in skipped:
        typer.echo(f"  skipped  {directory / name}  (already exists, use --force to overwrite)")

    if created:
        typer.echo(f"\nNext steps:")
        typer.echo(f"  1. Edit {plan_file} — add your tasks")
        typer.echo(f"  2. Edit {prompt_file} — adjust instructions if needed")
        typer.echo(f"  3. Run: ocloop run --model <provider/model>")
