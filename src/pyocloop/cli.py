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
    no_args_is_help=False,
)


@app.command()
def main(
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
        False, "--verbose", help="Enable verbose logging"
    ),
    log: Optional[Path] = typer.Option(
        None, "--log", help="Write all log entries to this file"
    ),
) -> None:
    # Capture cwd before resolve() — used as x-opencode-directory to scope SSE events
    directory = os.getcwd()

    # Path bug fix: resolve all paths to absolute immediately
    prompt_abs = prompt.resolve()
    plan_abs = plan.resolve()

    if not debug:
        if not plan_abs.exists():
            typer.echo(f"Error: Plan file not found: {plan_abs}", err=True)
            typer.echo(
                f"\nCreate a {plan_abs.name} file with a task list, for example:\n"
                "  ## Backlog\n"
                "  - [ ] Task one description\n"
                "  - [ ] Task two description\n",
                err=True,
            )
            raise typer.Exit(1)

        if not prompt_abs.exists():
            typer.echo(f"Error: Prompt file not found: {prompt_abs}", err=True)
            typer.echo(
                "\nCreate a prompt file with instructions for executing plan tasks.\n"
                "Use {{PLAN_FILE}} as a placeholder for the plan file path.\n",
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
