"""CLI entry point for kimi-mneme."""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import click

from mneme import __version__
from mneme.compat import fix_windows_encoding
from mneme.config import load_config
from mneme.targets import CLAUDE, claude_target, detect_target_name, write_marker
from mneme.updater import is_update_available, print_update_notice, upgrade_package

fix_windows_encoding()


def get_project_root() -> Path:
    """Get the project root directory (where mneme package lives).

    When installed via uvx/pip, plugin files are in the package directory.
    When running from source, they're in the repo root.
    """
    # Package directory (where mneme/ is installed)
    package_dir = Path(__file__).parent.parent.resolve()

    # Check if plugin directory exists alongside the package (source install)
    source_plugin = package_dir / "plugin"
    if source_plugin.exists():
        return package_dir

    # When installed via uvx/pip, plugin files are inside the package
    # Look for plugin in the installed package
    installed_plugin = Path(__file__).parent / "plugin"
    if installed_plugin.exists():
        return Path(__file__).parent

    # Fallback: return package dir and let caller handle missing plugin
    return package_dir


def get_kimi_dir() -> Path:
    """Get the Kimi CLI configuration directory."""
    return Path.home() / ".kimi-code"


def get_mneme_dir() -> Path:
    """Get the mneme data directory for the active target.

    Resolves to ``~/.kimi-code/mneme`` or ``~/.claude/mneme`` depending on the
    active target (env ``MNEME_TARGET`` / marker / auto-detect). ``bootstrap``
    sets ``MNEME_TARGET`` up front so every step writes to the right place.
    """
    from mneme.targets import active_target

    return active_target().data_dir


def _mneme_python() -> str:
    """Return the most stable Python interpreter that has mneme installed.

    Prefers the persistent uv-tool interpreter (survives uvx cache purges) over
    an ephemeral ``sys.executable``.
    """
    candidates = [
        Path.home()
        / "AppData"
        / "Roaming"
        / "uv"
        / "tools"
        / "mneme-kimi-code"
        / "Scripts"
        / "python.exe",
        Path.home() / ".local" / "share" / "uv" / "tools" / "mneme-kimi-code" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON to ``path`` atomically (temp file + os.replace)."""
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


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
        ("Database", _init_database),
        ("Configuration", _create_default_config),
        ("Hooks", _register_hooks),
        ("MCP Server", _register_mcp),
        ("Skills", _install_skills),
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
        _init_database()

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


def _init_database() -> bool:
    """Initialize the SQLite database."""
    click.echo("  Initializing database...")
    mneme_dir = get_mneme_dir()
    mneme_dir.mkdir(parents=True, exist_ok=True)
    db_path = mneme_dir / "mneme.db"

    try:
        from mneme.db.schema import init_db

        init_db(str(db_path))
        click.echo(f" Database initialized at {db_path}")
        return True
    except Exception as e:
        click.echo(f" Failed to initialize database: {e}")
        return False


def _create_default_config() -> bool:
    """Create default configuration file."""
    click.echo("  Creating default configuration...")
    config_dir = get_mneme_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    if config_path.exists():
        click.echo("  Config already exists, skipping")
        return True

    default_config = {
        "db": {"path": str(config_dir / "mneme.db")},
        "llm": {
            "provider": "kimi",
            "model": "kimi-k2.5",
        },
        "compression": {
            "enabled": True,
        },
        "server": {"port": 37777},
    }

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=2)
        click.echo(f" Config created at {config_path}")
        return True
    except Exception as e:
        click.echo(f" Failed to create config: {e}")
        return False


def _register_hooks() -> bool:
    """Register hooks in Kimi CLI config.

    Hooks are copied to ~/.kimi-code/mneme/hooks/ so they survive uvx cache
    purges and remain at a stable path.
    """
    click.echo("Registering hooks...")

    kimi_config = get_kimi_dir() / "config.toml"
    project_root = get_project_root()
    source_hooks_dir = project_root / "hooks"

    # Copy hooks to a stable location inside ~/.kimi-code/mneme/hooks
    stable_hooks_dir = get_mneme_dir() / "hooks"
    stable_hooks_dir.mkdir(parents=True, exist_ok=True)

    # Wire watcher (inside session_start.py) now indexes all session data
    # from ~/.kimi-code/sessions/<hash>/<id>/wire.jsonl, making UserPromptSubmit,
    # PostToolUse and PostToolUseFailure hooks redundant.
    hooks = [
        ("SessionStart", "session_start.py"),
        ("SessionEnd", "session_end.py"),
        ("PreCompact", "pre_compact.py"),
        ("PostCompact", "post_compact.py"),
    ]

    for _, script in hooks:
        src = source_hooks_dir / script
        dst = stable_hooks_dir / script
        if src.exists():
            shutil.copy2(src, dst)

    # Write version file for hooks version checking
    try:
        from mneme import __version__

        version_file = stable_hooks_dir / ".version"
        version_file.write_text(__version__, encoding="utf-8")
    except Exception:
        pass

    # Prefer the persistent installed mneme python (has mneme installed and
    # survives uvx cache purges) over an ephemeral sys.executable.
    python_exe = sys.executable
    uv_tool_candidates = [
        Path.home()
        / "AppData"
        / "Roaming"
        / "uv"
        / "tools"
        / "mneme-kimi-code"
        / "Scripts"
        / "python.exe",
        Path.home() / ".local" / "share" / "uv" / "tools" / "mneme-kimi-code" / "bin" / "python",
    ]
    for candidate in uv_tool_candidates:
        if candidate.exists():
            python_exe = str(candidate)
            break

    hook_entries = []
    for event, script in hooks:
        script_path = stable_hooks_dir / script
        # Use forward slashes in paths to avoid TOML escape issues on Windows.
        # Wrap paths in quotes to handle spaces (e.g. "Program Files" on Windows).
        cmd = f'"{python_exe}" "{script_path}" --target kimi'.replace("\\", "/")
        hook_entries.append(f"[[hooks]]\nevent = \"{event}\"\ncommand = '{cmd}'\n")

    hook_block = (
        "\n# === mneme-kimi-code hooks ===\n"
        + "\n".join(hook_entries)
        + "# === end mneme-kimi-code hooks ===\n"
    )

    try:
        if kimi_config.exists():
            content = kimi_config.read_text(encoding="utf-8")

            # Backup original config
            backup_path = get_kimi_dir() / "config.toml.backup"
            shutil.copy2(kimi_config, backup_path)

            # Remove any existing mneme hook block (current or legacy name).
            for label in ("mneme-kimi-code", "kimi-mneme"):
                begin = f"# === {label} hooks ==="
                end_marker = f"# === end {label} hooks ==="
                if begin in content and end_marker in content:
                    click.echo("Hooks already registered, updating...")
                    start = content.find(begin)
                    end = content.find(end_marker) + len(end_marker)
                    content = content[:start] + content[end:]

            # Remove bare `hooks = []` which conflicts with [[hooks]] tables
            import re

            content = re.sub(r"\n?hooks\s*=\s*\[\]\s*\n?", "\n", content)

            content = content.rstrip() + "\n" + hook_block
        else:
            content = hook_block

        # NOTE: Do NOT use utf-8-sig (BOM) — tomlkit in Kimi CLI chokes on \ufeff
        with open(kimi_config, "w", encoding="utf-8") as f:
            f.write(content)

        click.echo(f"Hooks registered in {kimi_config}")
        return True

    except Exception as e:
        click.echo(f"Failed to register hooks: {e}")
        return False


def _install_plugin() -> bool:
    """Install the Kimi CLI plugin."""
    click.echo(" Installing plugin...")

    plugin_dir = get_project_root() / "plugin"

    if not plugin_dir.exists():
        click.echo(" Plugin directory not found")
        return False

    # Generate plugin.json with correct python executable
    _generate_plugin_json(plugin_dir)

    try:
        result = subprocess.run(
            ["kimi", "plugin", "install", str(plugin_dir)],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            click.echo(" Plugin installed")
            return True
        else:
            click.echo(f"  Plugin install output: {result.stdout or result.stderr}")
            # Don't fail — plugin might already be installed
            return True

    except FileNotFoundError:
        click.echo("  Kimi CLI not found in PATH. Please install plugin manually:")
        click.echo(f"   kimi plugin install {plugin_dir}")
        return True
    except Exception as e:
        click.echo(f" Failed to install plugin: {e}")
        return False


def _generate_plugin_json(plugin_dir: Path) -> None:
    """Generate plugin.json using mneme CLI commands (stable across installs)."""
    plugin_json = {
        "name": "mneme-kimi-code",
        "version": __version__,
        "description": "Persistent memory plugin for Kimi Code CLI — search and retrieve past session context. Part of the kimi-plugins ecosystem.",
        "config_file": "config.json",
        "inject": {
            "llm.api_key": "api_key",
            "llm.endpoint": "base_url",
        },
        "tools": [
            {
                "name": "mneme_search",
                "description": "Search memory index with full-text queries. Returns compact index with IDs, timestamps, types, and snippets. Use this as the first step in progressive disclosure. Searches across raw observations, structured observations, and wire events. Semantic search available via --semantic flag (slower, requires embeddings).",
                "command": ["mneme", "search", "--query", "{{query}}"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query — natural language or keywords",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["bugfix", "feature", "refactor", "docs", "test"],
                            "description": "Filter by observation type",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 10,
                            "description": "Maximum results (default: 10, max: 50)",
                        },
                        "date_from": {
                            "type": "string",
                            "description": "ISO date filter (inclusive), e.g. 2026-04-01",
                        },
                        "date_to": {
                            "type": "string",
                            "description": "ISO date filter (inclusive), e.g. 2026-05-01",
                        },
                        "project": {
                            "type": "string",
                            "description": "Filter by project/directory name",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "mneme_timeline",
                "description": "Get chronological context around a specific observation. Shows what happened before and after. Use after mneme_search to understand context.",
                "command": ["mneme", "timeline", "--observation-id", "{{observation_id}}"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "observation_id": {
                            "type": "integer",
                            "description": "Center observation ID from search results",
                        },
                        "radius": {
                            "type": "integer",
                            "default": 5,
                            "description": "Number of observations before/after (default: 5, max: 20)",
                        },
                    },
                    "required": ["observation_id"],
                },
            },
            {
                "name": "mneme_get",
                "description": "Fetch full observation details by IDs. Always batch multiple IDs in one call. Use as the final step after identifying relevant observations.",
                "command": ["mneme", "get", "--ids", "{{ids}}"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ids": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Array of observation IDs to fetch",
                        },
                    },
                    "required": ["ids"],
                },
            },
        ],
    }

    with open(plugin_dir / "plugin.json", "w", encoding="utf-8") as f:
        json.dump(plugin_json, f, indent=2)


def _register_mcp() -> bool:
    """Register kimi-mneme MCP server in Kimi CLI config."""
    click.echo(" Registering MCP server...")

    mcp_config = get_kimi_dir() / "mcp.json"

    mcp_entry = {
        "mneme-kimi-code": {
            "command": sys.executable,
            "args": ["-m", "mneme.mcp_server"],
            "env": {"MNEME_TARGET": "kimi"},
        }
    }

    try:
        if mcp_config.exists():
            with open(mcp_config, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"mcpServers": {}}

        if "mcpServers" not in data:
            data["mcpServers"] = {}

        # Drop the legacy key from older installs to avoid duplicate servers.
        data["mcpServers"].pop("kimi-mneme", None)
        data["mcpServers"]["mneme-kimi-code"] = mcp_entry["mneme-kimi-code"]

        with open(mcp_config, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        click.echo(f" MCP server registered in {mcp_config}")
        return True

    except Exception as e:
        click.echo(f" Failed to register MCP: {e}")
        return False


def _install_skills() -> bool:
    """Copy skill files to Kimi CLI skills directory."""
    click.echo(" Installing skills...")

    project_root = get_project_root()
    source_skills = project_root / "skills"

    if not source_skills.exists():
        click.echo(" No skills directory found, skipping")
        return True

    # Kimi CLI skills directory
    kimi_skills = get_kimi_dir() / "skills"
    kimi_skills.mkdir(parents=True, exist_ok=True)

    try:
        for skill_dir in source_skills.iterdir():
            if skill_dir.is_dir():
                dst = kimi_skills / skill_dir.name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(skill_dir, dst)
                click.echo(f"  Installed skill: {skill_dir.name}")

        click.echo(f" Skills installed to {kimi_skills}")
        return True

    except Exception as e:
        click.echo(f" Failed to install skills: {e}")
        return False


# ---------------------------------------------------------------------------
# Claude Code registration (settings.json hooks, ~/.claude.json MCP, skills)
# ---------------------------------------------------------------------------

# Hook scripts shared between targets. The Kimi wire watcher / Claude transcript
# watcher (running in the server) capture per-tool and per-prompt data, so only
# these lifecycle hooks need registering directly.
_CLAUDE_HOOKS = [
    ("SessionStart", "session_start.py"),
    ("SessionEnd", "session_end.py"),
    ("PreCompact", "pre_compact.py"),
    ("PostCompact", "post_compact.py"),
]


def _copy_hook_scripts(stable_hooks_dir: Path) -> None:
    """Copy hook scripts (+ package marker) into a stable per-target location."""
    source_hooks_dir = get_project_root() / "hooks"
    stable_hooks_dir.mkdir(parents=True, exist_ok=True)
    scripts = {script for _, script in _CLAUDE_HOOKS} | {"__init__.py"}
    for script in scripts:
        src = source_hooks_dir / script
        if src.exists():
            shutil.copy2(src, stable_hooks_dir / script)
    with contextlib.suppress(Exception):
        (stable_hooks_dir / ".version").write_text(__version__, encoding="utf-8")


def _register_hooks_claude() -> bool:
    """Register lifecycle hooks in Claude Code's ``settings.json`` (merge, not clobber)."""
    click.echo("Registering hooks (Claude Code)...")

    target = claude_target()
    settings_path = target.config_dir / "settings.json"
    stable_hooks_dir = target.data_dir / "hooks"
    _copy_hook_scripts(stable_hooks_dir)

    python_exe = _mneme_python()
    marker = str(stable_hooks_dir).replace("\\", "/").lower()

    def _is_mneme_group(group: dict) -> bool:
        try:
            for hook in group.get("hooks", []):
                cmd = str(hook.get("command", "")).replace("\\", "/").lower()
                if marker in cmd or "mneme" in cmd:
                    return True
        except Exception:
            pass
        return False

    try:
        if settings_path.exists():
            shutil.copy2(settings_path, settings_path.with_name("settings.json.mneme-backup"))
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        else:
            settings = {}
        if not isinstance(settings, dict):
            settings = {}

        hooks_cfg = settings.get("hooks")
        if not isinstance(hooks_cfg, dict):
            hooks_cfg = {}

        for event, script in _CLAUDE_HOOKS:
            script_path = stable_hooks_dir / script
            command = f'"{python_exe}" "{script_path}" --target claude'
            existing = hooks_cfg.get(event)
            groups = [
                g
                for g in (existing if isinstance(existing, list) else [])
                if isinstance(g, dict) and not _is_mneme_group(g)
            ]
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 30}]})
            hooks_cfg[event] = groups

        settings["hooks"] = hooks_cfg
        _atomic_write_json(settings_path, settings)
        click.echo(f" Hooks registered in {settings_path}")
        return True

    except Exception as e:
        click.echo(f" Failed to register Claude hooks: {e}")
        return False


def _register_mcp_claude() -> bool:
    """Register the mneme MCP server (user scope) in ``~/.claude.json`` (merge)."""
    click.echo(" Registering MCP server (Claude Code)...")

    # The user-scope MCP config lives at ~/.claude.json (home), not inside
    # ~/.claude/.
    claude_json = Path.home() / ".claude.json"
    entry = {
        "type": "stdio",
        "command": _mneme_python(),
        "args": ["-m", "mneme.mcp_server"],
        "env": {"MNEME_TARGET": "claude"},
    }

    try:
        if claude_json.exists():
            shutil.copy2(claude_json, claude_json.with_name(".claude.json.mneme-backup"))
            with open(claude_json, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}

        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        # Drop any legacy Kimi-named server to avoid duplicates.
        servers.pop("mneme-kimi-code", None)
        servers["mneme"] = entry
        data["mcpServers"] = servers

        _atomic_write_json(claude_json, data)
        click.echo(f" MCP server 'mneme' registered in {claude_json}")
        return True

    except Exception as e:
        click.echo(f" Failed to register Claude MCP: {e}")
        return False


def _install_skills_claude() -> bool:
    """Copy skill files into Claude Code's user skills directory."""
    click.echo(" Installing skills (Claude Code)...")

    source_skills = get_project_root() / "skills"
    if not source_skills.exists():
        click.echo(" No skills directory found, skipping")
        return True

    dest = claude_target().config_dir / "skills"
    dest.mkdir(parents=True, exist_ok=True)

    try:
        for skill_dir in source_skills.iterdir():
            if skill_dir.is_dir():
                target_dir = dest / skill_dir.name
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                shutil.copytree(skill_dir, target_dir)
                click.echo(f"  Installed skill: {skill_dir.name}")
        click.echo(f" Skills installed to {dest}")
        return True
    except Exception as e:
        click.echo(f" Failed to install Claude skills: {e}")
        return False


def _start_server() -> bool:
    """Start the web server."""
    from mneme.config import load_config

    config = load_config()
    server_cfg = config.get("server", {})

    if not server_cfg.get("auto_start", True):
        click.echo(" Auto-start disabled in config")
        return True

    host = server_cfg.get("host", "127.0.0.1")
    port = server_cfg.get("port", 37777)

    # Check if already running
    import socket

    try:
        with socket.create_connection((host, port), timeout=1):
            click.echo(f" Server already running at http://{host}:{port}")
            return True
    except OSError:
        pass  # Not running, start it

    click.echo(" Starting web server...")

    log_file = get_mneme_dir() / "server.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Always redirect stdout/stderr to a log file — using DEVNULL or PIPE
        # on Windows can cause the child process to die when the parent exits.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write("\n--- server start ---\n")
            lf.flush()

            if sys.platform == "win32":
                # Windows: CREATE_NEW_PROCESS_GROUP is critical — without it the
                # server receives CTRL_BREAK_EVENT when the parent console closes
                # and dies immediately.  CREATE_NO_WINDOW avoids a visible console.
                subprocess.Popen(
                    [sys.executable, "-m", "mneme.server"],
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                )
            elif sys.platform == "darwin":
                # macOS: use nohup-style backgrounding via subprocess
                subprocess.Popen(
                    [sys.executable, "-m", "mneme.server"],
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            else:
                # Linux and other Unix: redirect to log file for debugging
                subprocess.Popen(
                    [sys.executable, "-m", "mneme.server"],
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

        click.echo(f" Web server started at http://{host}:{port}")
        return True

    except Exception as e:
        click.echo(f"  Failed to start server: {e}")
        click.echo("   Start manually: python -m mneme.server")
        return True


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
        ("Database", _init_database),
        ("Configuration", _create_default_config),
    ]

    if chosen == CLAUDE:
        steps.append(("Hooks", _register_hooks_claude))
        steps.append(("MCP Server", _register_mcp_claude))
        steps.append(("Skills", _install_skills_claude))
    else:
        steps.append(("Hooks", _register_hooks))
        # The npm Kimi Code CLI has no `kimi plugin install`; the plugin step is
        # a no-op there, so it is opt-in via --with-plugin. Integration runs via
        # the hooks (above) and the MCP server (below).
        if with_plugin:
            steps.append(("Plugin", _install_plugin))
        steps.append(("MCP Server", _register_mcp))
        steps.append(("Skills", _install_skills))

    if not no_server:
        steps.append(("Server", _start_server))

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
