"""Host-CLI integration / installer steps for mneme.

Each ``register_*`` / ``install_*`` step returns ``True`` on success (or benign
skip) and ``False`` on failure, and prints its own progress via click. They are
idempotent and safe to re-run — that is what ``mneme bootstrap`` / ``update``
rely on. Extracted from ``cli.py`` so this host-integration logic (the part of
the install path that historically broke) is unit-testable and reusable.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from mneme import __version__
from mneme.core.paths import (
    atomic_write_json,
    get_kimi_dir,
    get_mneme_dir,
    get_project_root,
    mneme_python,
)
from mneme.targets import claude_target

# Hook scripts shared between targets. The Kimi wire watcher / Claude transcript
# watcher (running in the server) capture per-tool and per-prompt data, so only
# these lifecycle hooks need registering directly.
_CLAUDE_HOOKS = [
    ("SessionStart", "session_start.py"),
    ("SessionEnd", "session_end.py"),
    ("PreCompact", "pre_compact.py"),
    ("PostCompact", "post_compact.py"),
]


def init_database() -> bool:
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


def create_default_config() -> bool:
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


def register_hooks() -> bool:
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
        version_file = stable_hooks_dir / ".version"
        version_file.write_text(__version__, encoding="utf-8")
    except Exception:
        pass

    # Prefer the persistent installed mneme python (survives uvx cache purges).
    python_exe = mneme_python()

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
            content = re.sub(r"\n?hooks\s*=\s*\[\]\s*\n?", "\n", content)

            content = content.rstrip() + "\n" + hook_block
        else:
            content = hook_block

        # NOTE: Do NOT use utf-8-sig (BOM) — tomlkit in Kimi CLI chokes on
        with open(kimi_config, "w", encoding="utf-8") as f:
            f.write(content)

        click.echo(f"Hooks registered in {kimi_config}")
        return True

    except Exception as e:
        click.echo(f"Failed to register hooks: {e}")
        return False


def build_plugin_json() -> dict[str, Any]:
    """Build the Kimi plugin manifest (pure — no I/O)."""
    return {
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


def generate_plugin_json(plugin_dir: Path) -> None:
    """Write plugin.json into ``plugin_dir`` (uses the current package version)."""
    with open(plugin_dir / "plugin.json", "w", encoding="utf-8") as f:
        json.dump(build_plugin_json(), f, indent=2)


def install_plugin() -> bool:
    """Install the Kimi CLI plugin."""
    click.echo(" Installing plugin...")

    plugin_dir = get_project_root() / "plugin"

    if not plugin_dir.exists():
        click.echo(" Plugin directory not found")
        return False

    # Generate plugin.json with correct python executable
    generate_plugin_json(plugin_dir)

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


def register_mcp() -> bool:
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


def install_skills() -> bool:
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


def copy_hook_scripts(stable_hooks_dir: Path) -> None:
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


def register_hooks_claude() -> bool:
    """Register lifecycle hooks in Claude Code's ``settings.json`` (merge, not clobber)."""
    click.echo("Registering hooks (Claude Code)...")

    target = claude_target()
    settings_path = target.config_dir / "settings.json"
    stable_hooks_dir = target.data_dir / "hooks"
    copy_hook_scripts(stable_hooks_dir)

    python_exe = mneme_python()
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
        atomic_write_json(settings_path, settings)
        click.echo(f" Hooks registered in {settings_path}")
        return True

    except Exception as e:
        click.echo(f" Failed to register Claude hooks: {e}")
        return False


def _claude_json_path() -> Path:
    """Path to Claude Code's user-scope config (``~/.claude.json``, not inside ~/.claude)."""
    return Path.home() / ".claude.json"


def register_mcp_claude() -> bool:
    """Register the mneme MCP server (user scope) in ``~/.claude.json`` (merge)."""
    click.echo(" Registering MCP server (Claude Code)...")

    claude_json = _claude_json_path()
    entry = {
        "type": "stdio",
        "command": mneme_python(),
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

        atomic_write_json(claude_json, data)
        click.echo(f" MCP server 'mneme' registered in {claude_json}")
        return True

    except Exception as e:
        click.echo(f" Failed to register Claude MCP: {e}")
        return False


def install_skills_claude() -> bool:
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


def start_server() -> bool:
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
