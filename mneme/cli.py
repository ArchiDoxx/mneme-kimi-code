"""CLI entry point for kimi-mneme."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from pathlib import Path

import click

from mneme import __version__
from mneme.compat import fix_windows_encoding
from mneme.config import load_config
from mneme.core import installer
from mneme.core.paths import get_kimi_dir, get_mneme_dir
from mneme.targets import CLAUDE, detect_target_name, write_marker
from mneme.updater import is_update_available, print_update_notice, upgrade_package

fix_windows_encoding()


@click.group()
@click.version_option(version=__version__, prog_name="mneme")
@click.option("--no-update-check", is_flag=True, help="Skip version check on startup")
def main(no_update_check: bool) -> None:
    """kimi-mneme — Persistent memory for Kimi Code CLI."""
    if not no_update_check:
        try:
            available, latest = is_update_available()
            if available and latest:
                print_update_notice(latest)
        except Exception:
            pass  # Silently fail if offline or PyPI unreachable


@main.command()
@click.option("--port", default=37777, help="Server port")
@click.option("--host", default="127.0.0.1", help="Server host")
def server(port: int, host: str) -> None:
    """Start the web server."""
    import uvicorn

    from mneme.server.app import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port)


# ---------------------------------------------------------------------------
# Plugin tool commands — called by Kimi CLI plugin system
# ---------------------------------------------------------------------------


def _get_plugin_credentials() -> tuple[str | None, str | None]:
    """Get credentials from env vars injected by Kimi CLI at plugin tool runtime.

    Kimi CLI injects fresh OAuth tokens as env vars when executing plugin tools:
      - llm.api_key  → fresh access token
      - llm.endpoint → API base URL

    This is DIFFERENT from the static config in ~/.kimi-code/plugins/kimi-mneme/config.json
    which contains a stale token from installation time.
    """
    import os

    # Env vars injected by Kimi CLI at plugin tool runtime
    # noqa: SIM112 — Kimi CLI uses lowercase env var names
    token = os.getenv("llm.api_key")  # noqa: SIM112
    endpoint = os.getenv("llm.endpoint")  # noqa: SIM112
    if token:
        return token, endpoint

    return None, None


def _maybe_structure_pending() -> None:
    """Lazy structuring: process pending messages when plugin tool is called.

    This ONLY works when called from a Kimi CLI plugin tool execution context,
    because Kimi CLI injects fresh llm.api_key as an env var at runtime.
    Background worker and manual CLI commands do NOT have this token.
    """
    plugin_token, base_url = _get_plugin_credentials()
    if not plugin_token:
        return  # Not in plugin tool context — skip

    try:
        from mneme.core.ai_provider import ConfigurableAIProvider, HybridProvider
        from mneme.db.store import ObservationStore
        from mneme.db.structured_store import StructuredObservationStore

        store = ObservationStore()
        structured_store = StructuredObservationStore()

        pending = store.claim_pending_messages(limit=10, message_type="observation")
        if not pending:
            return

        # Determine endpoint if not provided
        if not base_url:
            base_url = (
                "https://api.kimi.com/coding/v1/chat/completions"
                if len(plugin_token) > 500
                else "https://api.moonshot.cn/v1/chat/completions"
            )

        ai_provider = ConfigurableAIProvider(
            provider="kimi",
            api_key=plugin_token,
            base_url=base_url,
            timeout=30.0,
        )
        provider = HybridProvider(ai_provider=ai_provider)

        import asyncio

        async def _process() -> None:
            for msg in pending:
                try:
                    result = await provider.structure_observation(
                        tool_name=msg.get("tool_name"),
                        tool_input=msg.get("tool_input"),
                        tool_output=msg.get("tool_response"),
                        error=msg.get("error"),
                    )
                    if result and not result.skip:
                        from pathlib import PurePath

                        cwd = msg.get("cwd", "")
                        if cwd:
                            normalized = cwd.replace("\\", "/")
                            project = PurePath(normalized).name or cwd
                        else:
                            project = "unknown"

                        structured_store.add_structured(
                            result,
                            session_id=msg["session_id"],
                            project=project,
                            raw_observation_id=msg.get("raw_observation_id"),
                            source=result.source,
                            model=result.source,
                        )
                    store.mark_message_processed(msg["id"])
                except Exception:
                    store.mark_message_failed(msg["id"])

        asyncio.run(_process())

    except Exception:
        pass


@main.command("search")
@click.option("--query", "-q", required=True, help="Search query")
@click.option("--limit", "-l", default=10, help="Max results")
@click.option("--date-from", help="Start date (ISO)")
@click.option("--date-to", help="End date (ISO)")
@click.option("--project", "-p", help="Project filter")
@click.option("--type", "obs_type", help="Observation type filter")
@click.option(
    "--semantic", is_flag=True, help="Enable semantic search (slower, requires embeddings)"
)
def search_cmd(
    query: str,
    limit: int,
    date_from: str | None,
    date_to: str | None,
    project: str | None,
    obs_type: str | None,
    semantic: bool,
) -> None:
    """Search memory index (plugin tool wrapper)."""
    _maybe_structure_pending()
    from mneme.core.query import SearchService

    output = SearchService().search(
        query,
        limit=limit,
        date_from=date_from,
        date_to=date_to,
        project=project,
        obs_type=obs_type,
        semantic=semantic,
    )
    click.echo(json.dumps(output, ensure_ascii=False, indent=2))


@main.command("timeline")
@click.option("--observation-id", "-i", type=int, required=True, help="Center observation ID")
@click.option("--radius", "-r", default=5, help="Items before/after")
def timeline_cmd(observation_id: int, radius: int) -> None:
    """Get chronological context around an observation (plugin tool wrapper)."""
    _maybe_structure_pending()
    from mneme.core.query import SearchService

    output = SearchService().timeline_raw(observation_id, radius)
    click.echo(json.dumps(output, ensure_ascii=False, indent=2))


@main.command("get")
@click.option("--ids", "-i", required=True, help="Comma-separated observation IDs")
def get_cmd(ids: str) -> None:
    """Fetch full observation details by IDs (plugin tool wrapper)."""
    _maybe_structure_pending()
    from mneme.core.query import SearchService

    id_list = [int(x.strip()) for x in ids.split(",") if x.strip()]
    output = SearchService().get_observations(id_list)
    click.echo(json.dumps(output, ensure_ascii=False, indent=2))


@main.command()
@click.option("--upgrade", "do_upgrade", is_flag=True, help="Upgrade package from PyPI")
def update(do_upgrade: bool) -> None:
    """Update hooks, config, or upgrade package from PyPI."""
    if do_upgrade:
        click.echo("⬆️  Upgrading mneme-kimi-code from PyPI...")
        if upgrade_package():
            click.echo("✅ Upgrade complete!")
            click.echo("Please restart for changes to take effect.")
        else:
            click.echo("❌ Upgrade failed. Try: pip install --upgrade mneme-kimi-code")
        return

    click.echo("🔄 Updating mneme-kimi-code hooks and config...")

    # Re-run bootstrap steps
    steps = [
        ("Database", installer.init_database),
        ("Configuration", installer.create_default_config),
        ("Hooks", installer.register_hooks),
        ("MCP Server", installer.register_mcp),
        ("Skills", installer.install_skills),
    ]

    for name, step in steps:
        click.echo(f"\n Step: {name}")
        click.echo("-" * 30)
        if not step():
            click.echo(f"  Step '{name}' had issues, continuing...")

    click.echo("\n✅ Update complete!")
    click.echo("Please restart Kimi CLI for changes to take effect.")


@main.command()
def init() -> None:
    """Initialize the database."""
    from mneme.config import load_config
    from mneme.db.schema import init_db

    config = load_config()
    init_db(config["db"]["path"])
    click.echo(" Database initialized")


@main.command()
@click.option("--days", default=0, help="Only sessions modified in the last N days (0 = all)")
def reindex(days: int) -> None:
    """Backfill the memory database from existing Kimi Code CLI sessions.

    The live watcher only ingests sessions that change while it runs, so this
    rebuilds history after an install/upgrade. Reads every wire.jsonl under
    ~/.kimi-code/sessions/<wd>/<session>/agents/<agent>/ and resolves the real
    session id + working directory from session_index.jsonl.

    Run on a fresh database (`mneme reset --force`) to avoid duplicate rows.
    """
    import time as _time

    from mneme.wire.indexer import WireIndexer
    from mneme.wire.reader import SessionReader, iter_session_wires, load_workdir_map

    sessions_dir = get_kimi_dir() / "sessions"
    if not sessions_dir.exists():
        click.echo(f" No sessions directory at {sessions_dir}")
        return

    workdir_map = load_workdir_map(get_kimi_dir())
    indexer = WireIndexer()
    cutoff = (_time.time() - days * 86400) if days else 0.0

    sessions = events = skipped = 0
    for identity in iter_session_wires(sessions_dir, workdir_map):
        try:
            if cutoff and identity.wire_path.stat().st_mtime < cutoff:
                skipped += 1
                continue
        except OSError:
            pass
        indexer.store.ensure_session(identity.session_id, identity.cwd)
        reader = SessionReader(
            identity.session_dir, identity.session_id, wire_path=identity.wire_path
        )
        counts = indexer.index_events(reader.read_new_events())
        indexer.index_state(reader.read_state())
        n = sum(counts.values())
        events += n
        sessions += 1
        click.echo(f"  {identity.session_id[:40]}: {n} events (cwd={identity.cwd or '?'})")

    click.echo(f"\n Reindexed {sessions} sessions, {events} events ({skipped} skipped).")


@main.command()
@click.option("--days", default=30, help="Delete observations older than N days")
def cleanup(days: int) -> None:
    """Clean up old observations."""
    import sqlite3

    from mneme.config import load_config

    config = load_config()
    db_path = config["db"]["path"]

    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "DELETE FROM observations WHERE created_at < datetime('now', '-' || ? || ' days')",
        (days,),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    click.echo(f"  Deleted {deleted} observations older than {days} days")


@main.command()
def stats() -> None:
    """Show database statistics."""
    from mneme.db.store import ObservationStore
    from mneme.db.structured_store import StructuredObservationStore

    store = ObservationStore()
    data = store.get_stats()

    click.echo(" mneme-kimi-code Statistics")
    click.echo("-" * 30)
    click.echo(f"Sessions:      {data['total_sessions']}")
    click.echo(f"Observations:  {data['total_observations']}")
    click.echo(f"Summaries:     {data['total_summaries']}")
    click.echo(f"DB Size:       {data['db_size_mb']} MB")

    if data.get("top_projects"):
        click.echo("\nTop Projects:")
        for p in data["top_projects"]:
            click.echo(f"  {p['project']}: {p['count']} sessions")

    # Structured observations stats
    try:
        structured_store = StructuredObservationStore()
        so_stats = structured_store.get_stats()
        click.echo(f"\nStructured:    {so_stats['total']}")
        if so_stats.get("by_source"):
            click.echo("  By source:")
            for s in so_stats["by_source"]:
                click.echo(f"    {s['source']}: {s['count']}")
        if so_stats.get("by_type"):
            click.echo("  By type:")
            for t in so_stats["by_type"][:5]:
                click.echo(f"    {t['type']}: {t['count']}")
    except Exception:
        pass


@main.command("structure")
@click.option("--limit", "-l", default=10, help="Max observations to process")
@click.option("--dry-run", is_flag=True, help="Show what would be processed without structuring")
def structure_cmd(limit: int, dry_run: bool) -> None:
    """Run AI structuring on pending observations manually.

    Requires LLM API key configured in ~/.kimi-code/mneme/config.json:
      { "llm": { "api_key": "your-key", "provider": "kimi" } }

    Heuristic structuring works automatically. This command is for AI-enhanced
    structuring when you have configured an API key.
    """
    import asyncio

    from mneme.config import load_config
    from mneme.core.worker import StructuringWorker

    config = load_config()
    llm_cfg = config.get("llm", {})
    api_key = llm_cfg.get("api_key")

    if not api_key:
        click.echo("❌ No API key configured.")
        click.echo("\nAdd to ~/.kimi-code/mneme/config.json:")
        click.echo('  { "llm": { "api_key": "your-key", "provider": "kimi" } }')
        click.echo("\nOr set env var: MNEME_LLM_API_KEY=your-key")
        click.echo("\nHeuristic structuring works without API key.")
        return

    worker = StructuringWorker()

    # Check if AI provider is actually enabled
    if not worker.provider.ai.enabled:
        click.echo("❌ AI provider is disabled or misconfigured.")
        click.echo(f"   Provider: {llm_cfg.get('provider', 'kimi')}")
        click.echo(f"   Model: {llm_cfg.get('model', 'default')}")
        return

    # Show pending count
    pending = worker.store.claim_pending_messages(limit=1, message_type="observation")
    # Return them since we only peeked
    if pending:
        for msg in pending:
            worker.store._get_conn().execute(
                "UPDATE pending_messages SET status = 'pending' WHERE id = ?",
                (msg["id"],),
            )

    count_result = (
        worker.store._get_conn()
        .execute(
            "SELECT COUNT(*) FROM pending_messages WHERE status = 'pending' AND message_type = 'observation'"
        )
        .fetchone()
    )
    pending_count = count_result[0] if count_result else 0

    click.echo(f"📦 Pending observations: {pending_count}")

    if dry_run:
        click.echo("\n--dry-run: would process these observations with AI structuring")
        return

    if pending_count == 0:
        click.echo("✅ Nothing to structure.")
        return

    click.echo(f"\n🚀 Starting AI structuring (limit={limit})...")
    click.echo(f"   Provider: {llm_cfg.get('provider', 'kimi')}")
    click.echo(f"   Model: {llm_cfg.get('model', 'default')}")
    click.echo("")

    async def _run() -> None:
        await worker._process_batch(limit=limit)

    try:
        asyncio.run(_run())
        click.echo("\n✅ Structuring complete!")
    except Exception as e:
        click.echo(f"\n❌ Structuring failed: {e}")


@main.command()
@click.option("--force", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--keep-sessions",
    is_flag=True,
    help="Keep session metadata, delete only observations and wire events",
)
def reset(force: bool, keep_sessions: bool) -> None:
    """Reset database — delete all data and start fresh.

    Wire traces on disk are preserved and will be re-indexed on next server start.
    Use --keep-sessions to preserve session list while clearing observations.
    """
    from mneme.config import load_config

    config = load_config()
    db_path = Path(config["db"]["path"])

    if not db_path.exists():
        click.echo(" Database does not exist, nothing to reset")
        return

    if not force:
        click.echo(" This will DELETE all data from the database!")
        click.echo(f"  DB: {db_path} ({db_path.stat().st_size / 1024 / 1024:.1f} MB)")
        click.echo("\n Wire traces in ~/.kimi-code/sessions/ will be preserved.")
        click.echo(" They will be re-indexed when the server starts.\n")

        if keep_sessions:
            click.echo(" Mode: --keep-sessions (session metadata preserved)")

        confirm = click.prompt("Type 'reset' to confirm", type=str)
        if confirm != "reset":
            click.echo(" Cancelled")
            return

    # Stop server if running
    click.echo(" Stopping server if running...")
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:37777/api/health", timeout=2)
        # Server is running, we can't safely delete while it's up
        click.echo(" Server is running. Please stop it first:")
        click.echo("   Get-Process python | Where-Object {$_.Path -like '*mneme*'} | Stop-Process")
        return
    except Exception:
        pass  # Server not running, safe to proceed

    if keep_sessions:
        # Delete only observations and wire data, keep sessions table
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        tables_to_clear = [
            "observations",
            "wire_events",
            "session_stats",
            "thinking",
            "assistant_messages",
            "session_todos",
            "session_summaries",
            "session_checkpoints",
            "compaction_events",
            "pending_messages",
            "observation_feedback",
            "patterns",
            "truncated_outputs",
            "user_prompts",
            "summaries",
        ]

        # Also clear FTS
        with contextlib.suppress(Exception):
            conn.execute("DELETE FROM observations_fts")

        for table in tables_to_clear:
            try:
                conn.execute(f"DELETE FROM {table}")
                click.echo(f"  Cleared {table}")
            except Exception as e:
                click.echo(f"  Could not clear {table}: {e}")

        conn.commit()
        conn.execute("VACUUM")
        conn.close()
        click.echo("\n Kept session metadata, cleared all observations and wire data")
    else:
        # Full reset — delete DB (sqlite-vec data is inside SQLite)
        try:
            db_path.unlink()
            wal = db_path.with_suffix(".db-wal")
            shm = db_path.with_suffix(".db-shm")
            for f in [wal, shm]:
                if f.exists():
                    f.unlink()
            click.echo(f"  Deleted {db_path}")
        except Exception as e:
            click.echo(f"  Failed to delete DB: {e}")
            return

        # Re-initialize empty database
        click.echo("\n Re-initializing database...")
        installer.init_database()

    new_size = db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0
    click.echo(f"\n Reset complete! DB size: {new_size:.1f} MB")
    click.echo("\n Next steps:")
    click.echo("  1. Start server: mneme server")
    click.echo("  2. Active sessions will be indexed in real-time")
    if not keep_sessions:
        click.echo("  3. Old sessions can be scanned via API or by enabling background scan")


# ---------------------------------------------------------------------------
# Bootstrap command — one-shot setup
# ---------------------------------------------------------------------------


@main.command()
@click.argument("query", required=False)
@click.option("--tables", is_flag=True, help="List all tables")
@click.option("--schema", metavar="TABLE", help="Show CREATE TABLE for a table")
@click.option("--file", type=click.Path(exists=True), help="Execute SQL from file")
@click.option("--interactive", "-i", is_flag=True, help="Open interactive SQL shell")
@click.option("--csv", is_flag=True, help="Output as CSV")
@click.option("--json-out", "json_out", is_flag=True, help="Output as JSON array")
def sql(
    query: str | None,
    tables: bool,
    schema: str | None,
    file: str | None,
    interactive: bool,
    csv: bool,
    json_out: bool,
) -> None:
    """Run SQL queries against the mneme SQLite database.

    Examples:
        mneme sql "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 5"
        mneme sql --tables
        mneme sql --schema sessions
        mneme sql --file script.sql
        mneme sql -i
    """
    config = load_config()
    db_path = config["db"]["path"]

    if not Path(db_path).exists():
        click.echo(f" Database not found: {db_path}")
        click.echo(" Run 'mneme bootstrap' first.")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        if tables:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            click.echo("Tables:")
            for r in rows:
                click.echo(f"  {r['name']}")
            return

        if schema:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (schema,),
            ).fetchone()
            if row and row["sql"]:
                click.echo(row["sql"])
            else:
                click.echo(f"Table '{schema}' not found.")
            return

        if file:
            sql_text = Path(file).read_text(encoding="utf-8")
            cursor = conn.execute(sql_text)
            _print_sql_results(cursor, csv=csv, json_out=json_out)
            conn.commit()
            return

        if interactive:
            _interactive_sql_shell(conn)
            return

        if not query:
            click.echo(click.get_current_context().get_help())
            return

        cursor = conn.execute(query)
        if cursor.description:
            _print_sql_results(cursor, csv=csv, json_out=json_out)
        else:
            conn.commit()
            click.echo(f" OK — rows affected: {cursor.rowcount}")
    except Exception as e:
        click.echo(f" Error: {e}", err=True)
        sys.exit(1)
    finally:
        conn.close()


def _print_sql_results(cursor, csv: bool, json_out: bool) -> None:
    """Print query results in various formats."""
    rows = cursor.fetchall()
    if not rows:
        click.echo("(no rows)")
        return

    headers = [d[0] for d in cursor.description]

    if json_out:
        import json

        result = [dict(row) for row in rows]
        click.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    if csv:
        import csv
        import io

        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(headers)
        writer.writerows(rows)
        click.echo(out.getvalue().rstrip("\n"))
        return

    # Pretty table
    str_rows = [[str(cell) if cell is not None else "NULL" for cell in row] for row in rows]
    col_widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    def _row_line(cells):
        return " | ".join(c.ljust(w) for c, w in zip(cells, col_widths, strict=False))

    click.echo(_row_line(headers))
    click.echo("-" * (sum(col_widths) + 3 * (len(headers) - 1)))
    for row in str_rows:
        click.echo(_row_line(row))
    click.echo(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")


def _interactive_sql_shell(conn: sqlite3.Connection) -> None:
    """Simple interactive SQL shell."""
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401

    click.echo("Interactive SQL shell. Type '.tables', '.schema TABLE', '.quit' or SQL.")
    while True:
        try:
            line = input("sqlite> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo("\nBye.")
            break

        if not line:
            continue
        if line in (".quit", ".q", ".exit"):
            click.echo("Bye.")
            break
        if line == ".tables":
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            for r in rows:
                click.echo(r["name"])
            continue
        if line.startswith(".schema "):
            table = line[8:].strip()
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row and row["sql"]:
                click.echo(row["sql"])
            else:
                click.echo(f"Table '{table}' not found.")
            continue

        try:
            cursor = conn.execute(line)
            if cursor.description:
                _print_sql_results(cursor, csv=False, json_out=False)
            else:
                conn.commit()
                click.echo(f"OK — rows affected: {cursor.rowcount}")
        except Exception as e:
            click.echo(f"Error: {e}")


@main.command()
@click.option(
    "--target",
    "target_opt",
    type=click.Choice(["auto", "kimi", "claude"]),
    default="auto",
    help="Host CLI to integrate with (auto-detects by default)",
)
@click.option("--no-server", is_flag=True, help="Don't start web server")
@click.option(
    "--with-plugin",
    is_flag=True,
    help="Also run the legacy `kimi plugin install` (Kimi target only; not supported on Kimi Code CLI)",
)
@click.option("--no-plugin", is_flag=True, help="Deprecated: plugin install is off by default")
def bootstrap(target_opt: str, no_server: bool, with_plugin: bool, no_plugin: bool) -> None:
    """One-shot setup for mneme.

    Registers hooks, the MCP server, the memory skill, initializes the database,
    and starts the web server for the chosen host CLI (Kimi Code CLI or Claude
    Code). Safe to run multiple times — idempotent.
    """
    import os

    from mneme import __version__

    chosen = detect_target_name() if target_opt == "auto" else target_opt
    # Pin the target for every step in this process and for later ad-hoc commands.
    os.environ["MNEME_TARGET"] = chosen
    write_marker(chosen)

    host_label = "Claude Code" if chosen == CLAUDE else "Kimi Code CLI"
    click.echo(f" Bootstrapping mneme for {host_label}...")
    click.echo(f" Version: {__version__}")
    click.echo(f" Target:  {chosen}  (data dir: {get_mneme_dir()})")
    click.echo(f" Python:  {sys.executable}")
    click.echo()

    steps = [
        ("Database", installer.init_database),
        ("Configuration", installer.create_default_config),
    ]

    if chosen == CLAUDE:
        steps.append(("Hooks", installer.register_hooks_claude))
        steps.append(("MCP Server", installer.register_mcp_claude))
        steps.append(("Skills", installer.install_skills_claude))
    else:
        steps.append(("Hooks", installer.register_hooks))
        # The npm Kimi Code CLI has no `kimi plugin install`; the plugin step is
        # a no-op there, so it is opt-in via --with-plugin. Integration runs via
        # the hooks (above) and the MCP server (below).
        if with_plugin:
            steps.append(("Plugin", installer.install_plugin))
        steps.append(("MCP Server", installer.register_mcp))
        steps.append(("Skills", installer.install_skills))

    if not no_server:
        steps.append(("Server", installer.start_server))

    all_ok = True
    for name, step in steps:
        click.echo(f"\n Step: {name}")
        click.echo("-" * 30)
        if not step():
            all_ok = False
            click.echo(f"  Step '{name}' had issues, continuing...")

    click.echo("\n" + "=" * 50)
    click.echo(f" mneme bootstrapped successfully for {host_label}!")
    click.echo("=" * 50)
    click.echo()
    click.echo("Next steps:")
    if chosen == CLAUDE:
        click.echo("  1. Restart Claude Code: claude")
    else:
        click.echo("  1. Restart Kimi CLI: kimi")
    click.echo("  2. Visit web UI: http://localhost:37777")
    click.echo("  3. Set your API key for AI compression:")
    click.echo("     export MOONSHOT_API_KEY=your-key")
    click.echo()
    click.echo("Commands:")
    click.echo("  mneme stats      Show database statistics")
    click.echo("  mneme server     Start web server")
    click.echo("  mneme cleanup    Clean old observations")
    click.echo("  mneme reset      Reset database (delete all data)")
    click.echo("  mneme sql        Run SQL queries against the database")
    click.echo("  mneme search     Search memory (plugin tool)")
    click.echo("  mneme timeline   Get timeline context (plugin tool)")
    click.echo("  mneme get        Fetch full details (plugin tool)")
    click.echo()
    click.echo("Files:")
    click.echo(f"  Config:  {get_mneme_dir() / 'config.json'}")
    click.echo(f"  DB:      {get_mneme_dir() / 'mneme.db'}")
    click.echo(f"  Logs:    {get_mneme_dir() / 'mneme.log'}")
    click.echo()

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
