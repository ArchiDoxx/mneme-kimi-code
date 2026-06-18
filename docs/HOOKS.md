# Hooks Reference

mneme-kimi-code uses Kimi Code CLI's [Hooks system](https://moonshotai.github.io/kimi-cli/) to
react to session lifecycle events. **Tool calls and user prompts are *not* captured via hooks** —
they are indexed directly from the session trace (`wire.jsonl`) by the wire watcher, which the
SessionStart hook starts. This keeps the hook surface small and avoids per-tool overhead.

## Registered Hooks

`mneme bootstrap` registers exactly **four** hooks in `~/.kimi-code/config.toml`. The hook commands
point at copies of the scripts under `~/.kimi-code/mneme/hooks/` (so they survive uvx cache purges),
using the Python interpreter that has `mneme` installed:

```toml
# === mneme-kimi-code hooks ===
[[hooks]]
event = "SessionStart"
command = '"/path/to/python" "~/.kimi-code/mneme/hooks/session_start.py"'

[[hooks]]
event = "SessionEnd"
command = '"/path/to/python" "~/.kimi-code/mneme/hooks/session_end.py"'

[[hooks]]
event = "PreCompact"
command = '"/path/to/python" "~/.kimi-code/mneme/hooks/pre_compact.py"'

[[hooks]]
event = "PostCompact"
command = '"/path/to/python" "~/.kimi-code/mneme/hooks/post_compact.py"'
# === end mneme-kimi-code hooks ===
```

Hooks receive their event payload as JSON on **stdin** (field `hook_event_name`, plus
`session_id`, `cwd`, …) and are fire-and-forget (they exit 0 even on error so they never block
the CLI).

## Hook Details

### SessionStart

**Trigger**: a new session is created or resumed.

**Actions**:
- Start the mneme web server (if not already running) and the **wire watcher**, which tails
  `~/.kimi-code/sessions/<wd>/<session>/agents/<agent>/wire.jsonl` and indexes tool calls,
  results, prompts and thinking in real time.
- Create the session record, check for checkpoints (resumed-after-compaction), query
  cross-session patterns and relevant past context, and inject it into the session via stdout.

### SessionEnd

**Trigger**: a session is closed.

**Actions**:
- Mark the session complete, trigger compression / session-summary generation, detect
  cross-session patterns, and stop the web server.

### PreCompact

**Trigger**: just before Kimi Code CLI compacts context.

**Actions**:
- Record the token count before compaction (for the compaction event log).

### PostCompact

**Trigger**: after Kimi Code CLI compacts context.

**Actions**:
- Record the compaction result (tokens before/after), extract key decisions and open tasks,
  and create a **session checkpoint** that is injected on the next SessionStart. This is what
  lets a session survive context compaction.

## Where tool/prompt data comes from

The npm Kimi Code CLI writes a flat, dot-notation wire format to `wire.jsonl`, e.g.
`turn.prompt`, `context.append_loop_event` (wrapping `tool.call` / `tool.result` /
`content.part` / `step.begin` / `step.end`), `usage.record` and `full_compaction.*`.
The wire watcher parses these (`mneme/wire/parser.py`) into observations — so there is no need
for `PostToolUse`, `PostToolUseFailure` or `UserPromptSubmit` hooks.

To backfill history (the live watcher only sees sessions that change while it runs), use:

```bash
mneme reindex            # all sessions
mneme reindex --days 7   # only sessions touched in the last 7 days
```

## Performance Notes

- Hooks run **fire-and-forget** and fail open (exit 0).
- The wire watcher indexes incrementally (byte offset per file) on the filesystem-watch thread.
- Checkpoint creation on PostCompact is lightweight.
