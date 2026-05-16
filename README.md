# pyocloop

A Python TUI that orchestrates [OpenCode](https://opencode.ai) to execute tasks from a `PLAN.md` file iteratively, one session at a time.

pyocloop is inspired by and compatible with [ocloop](https://github.com/d3vr/ocloop) by [@d3vr](https://github.com/d3vr). It reimplements the same concept in pure Python using [Textual](https://textual.textualize.io/), fixing TUI display issues and a path resolution bug present in the original.

## How it works

1. pyocloop starts an `opencode serve` subprocess
2. On each iteration it creates a session, sends your loop prompt (with the plan file path injected), and waits for the session to go idle
3. OpenCode reads the plan, executes the next task, marks it `[x]`, and optionally appends `<plan-complete>` when done
4. The TUI shows live progress: task counter, progress bar, current task, token count, and elapsed/average time per iteration
5. The loop stops when OpenCode writes `<plan-complete>` to the plan file

## Requirements

- Python 3.11+
- [OpenCode](https://opencode.ai) installed and configured (`opencode` on your PATH)

## Installation

```bash
git clone https://github.com/rbarzic/pyocloop
cd pyocloop
pip install .
```

For development (editable install):

```bash
pip install -e .
```

## Usage

```
ocloop [OPTIONS]

Options:
  -m, --model TEXT   Model to use (format: providerID/modelID).
                     List available models with: opencode models
  -a, --agent TEXT   Agent to use
  --prompt PATH      Path to loop prompt file  [default: .loop-prompt.md]
  --plan PATH        Path to plan file         [default: PLAN.md]
  -p, --port INT     OpenCode server port      [default: 4096]
  -r, --run          Start iterations immediately (no keypress needed)
  -d, --debug        Debug mode (skip plan file validation)
  --verbose          Log every SSE event in the activity panel
  --log PATH         Write all log entries to a file
  --help             Show this message and exit
```

## Key bindings

| Key       | Action                        |
|-----------|-------------------------------|
| `S`       | Start the loop                |
| `Space`   | Pause / Resume                |
| `R`       | Retry after an error          |
| `Q`       | Quit (aborts current session) |

## Example

The `examples/` directory contains a self-contained demo: answering EU capitals quiz questions.

**File layout:**

```
your-project/
├── PLAN.md              # task list (or use --plan to point elsewhere)
├── .loop-prompt.md      # instructions for OpenCode (or use --prompt)
└── ...
```

**`examples/PLAN.md`** — task list:

```markdown
# EU Capitals Quiz Plan

## Backlog

### Phase 1: Western Europe

- [ ] **1** What is the capital of Austria?
- [ ] **2** What is the capital of Belgium?
- [ ] **3** What is the capital of France?
...
```

**`examples/loop-prompt.md`** — loop prompt (note the `{{PLAN_FILE}}` placeholder):

```markdown
Execute the next task from {{PLAN_FILE}}.

Before starting:
1. Read {{PLAN_FILE}} fully

Task selection:
- Pick the FIRST uncompleted task
- Mark it [x] after completion

Completion check:
- If all tasks are [x] or [BLOCKED], append:
  <plan-complete>SUMMARY</plan-complete>
```

**Run:**

```bash
cd examples/
ocloop \
  --model openai/gpt-4.5 \
  --prompt ./loop-prompt.md \
  --plan ./PLAN.md \
  --run
```

Or with relative paths from any directory:

```bash
ocloop \
  --model openai/gpt-4.5 \
  --prompt /path/to/loop-prompt.md \
  --plan /path/to/PLAN.md
```

## Plan file format

```markdown
- [ ] Pending task
- [x] Completed task
- [MANUAL] Task requiring human intervention (skipped by the loop)
- [BLOCKED: reason] Task that could not be completed
```

The loop ends when OpenCode appends this tag to the plan file:

```
<plan-complete>Summary of what was done</plan-complete>
```

## Loop prompt tips

- Use `{{PLAN_FILE}}` as the placeholder — pyocloop replaces it with the absolute path at runtime
- Instruct OpenCode to mark tasks `[x]` after completion and append `<plan-complete>` when all done
- Keep prompts focused: one task per session works best for reliable progress tracking

## Architecture

pyocloop does **not** call any LLM directly. It delegates all AI work to the `opencode` binary:

```
ocloop (Textual TUI)
  └─ opencode serve  (subprocess)
       ├─ POST /session          create session
       ├─ POST /session/{id}/prompt_async  send prompt
       ├─ GET  /event?directory=…          SSE stream (progress events)
       └─ GET  /config                     detect active model
```
