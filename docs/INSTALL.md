# Installation Guide

> **Quick reference:** Die aktuellsten Installationsanweisungen stehen in der [README.md](../README.md). Diese Datei wiederholt die wichtigsten Schritte und ergänzt Konfigurationsdetails.

## Prerequisites

- **Python**: 3.10 or higher
- **Kimi Code CLI**: 1.41.0 or higher (`kimi --version`)
- **sqlite3 CLI**: Required for database inspection and internal operations. Install via your system package manager (`apt install sqlite3`, `brew install sqlite3`, `winget install SQLite.SQLite`, etc.)
- **pip** or **uv**: for Python package management

## Quick Install (recommended)

### Via `uvx` (one command, no permanent install)

```bash
uvx --from mneme-kimi-code mneme bootstrap
```

This single command:

- Installs all Python dependencies
- Registers hooks in `~/.kimi-code/config.toml`
- Registers the MCP server in `~/.kimi-code/mcp.json`
- Creates the SQLite database
- Starts the web server

### Via `uv tool` (recommended for daily use)

```bash
uv tool install mneme-kimi-code
mneme bootstrap
```

Update later:

```bash
uv tool upgrade mneme-kimi-code
mneme bootstrap
```

### Via `pip` (fallback)

```bash
pip install mneme-kimi-code
mneme bootstrap
```

Update later:

```bash
pip install --upgrade mneme-kimi-code
mneme bootstrap
```

### From source

```bash
# Original repository:
# git clone https://github.com/barrelc/kimi-mneme.git

# Dieser Fork:
git clone https://github.com/DEIN_USERNAME/mneme-kimi-code.git
cd mneme-kimi-code
pip install -e .
mneme bootstrap
```

---

## Important: Kimi Code CLI does not use `kimi plugin install`

Kimi Code CLI does **not** support the legacy `kimi plugin install` command. `mneme bootstrap` therefore registers integration in two places:

1. **Hooks** in `~/.kimi-code/config.toml` — lifecycle events (SessionStart, PostToolUse, SessionEnd, etc.)
2. **MCP server** in `~/.kimi-code/mcp.json` — exposes 15+ memory tools to Kimi Code CLI, Claude Desktop, Cursor, Goose, etc.

No manual plugin installation step is required or possible.

---

## Manual Install

If you prefer to understand each step:

### 1. Install Python Dependencies

```bash
pip install -e .
```

Or with uv:

```bash
uv pip install -e .
```

### 2. Initialize Database

```bash
mneme init
```

### 3. Register Hooks

```bash
mneme bootstrap --no-plugin --no-server
```

Or manually add to `~/.kimi-code/config.toml`:

```toml
[[hooks]]
event = "SessionStart"
command = "python /path/to/mneme-kimi-code/hooks/session_start.py"

[[hooks]]
event = "SessionEnd"
command = "python /path/to/mneme-kimi-code/hooks/session_end.py"

[[hooks]]
event = "PostToolUse"
command = "python /path/to/mneme-kimi-code/hooks/post_tool_use.py"

[[hooks]]
event = "PostToolUseFailure"
command = "python /path/to/mneme-kimi-code/hooks/post_tool_use_failure.py"

[[hooks]]
event = "UserPromptSubmit"
command = "python /path/to/mneme-kimi-code/hooks/user_prompt_submit.py"
```

> **Note**: Use `python` (not `python3`) — the bootstrap command automatically uses the correct Python executable for your system (`sys.executable`).

### 4. Register MCP Server (optional)

`mneme bootstrap` does this automatically. To do it manually, add to `~/.kimi-code/mcp.json`:

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "mneme",
      "args": ["mcp"]
    }
  }
}
```

If you have not installed `mneme` globally, use `uvx` instead:

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "uvx",
      "args": ["--from", "mneme-kimi-code", "mneme", "mcp"]
    }
  }
}
```

### 5. Start Web Server (optional)

```bash
mneme server
```

The server runs on `http://localhost:37777` by default.

### Platform Notes

**Windows**: The server is started with `CREATE_NEW_PROCESS_GROUP` flag so it survives when the parent console (e.g., Kimi Code CLI) exits. Logs are written to `~/.kimi-code/mneme/server.log`.

**macOS/Linux**: The server runs as a background process with `start_new_session=True`. Logs are written to `~/.kimi-code/mneme/server.log`.

---

## Configuration

Create `~/.kimi-code/mneme/config.json`:

```json
{
  "db": {
    "path": "~/.kimi-code/mneme/mneme.db"
  },
  "vector": {
    "path": "~/.kimi-code/mneme/vectors"
  },
  "llm": {
    "provider": "kimi",
    "model": "kimi-k2.5"
  },
  "compression": {
    "enabled": true
  },
  "injection": {
    "enabled": true,
    "max_tokens": 2000,
    "min_relevance": 0.7,
    "recency_boost_days": 7
  },
  "privacy": {
    "exclude_patterns": ["*.env*", "*secret*", "*password*"]
  }
}
```

### Per-Project Configuration

Create `.mneme.json` in your project root for project-specific settings:

```json
{
  "injection": {
    "max_tokens": 1000,
    "recency_boost_days": 14
  },
  "privacy": {
    "exclude_patterns": ["*.local.env", "secrets/"]
  }
}
```

Project config merges with global config (project values override global).

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MNEME_DB_PATH` | SQLite database path | `~/.kimi-code/mneme/mneme.db` |
| `MNEME_VECTOR_PATH` | Vector embeddings cache path | `~/.kimi-code/mneme/vectors` |
| `MNEME_SERVER_PORT` | Web server port | `37777` |
| `MNEME_LLM_PROVIDER` | LLM provider: `kimi`, `ollama`, `openai_compatible` | `kimi` |
| `MNEME_LLM_MODEL` | Model name | `kimi-k2.5` |
| `MNEME_LLM_BASE_URL` | Custom API base URL | — |
| `MNEME_LLM_API_KEY` | API key for LLM provider | — |
| `MNEME_LOG_LEVEL` | Logging level | `INFO` |
| `MNEME_SERVER_HOST` | Web server host | `127.0.0.1` |

---

## Verify Installation

```bash
# Check mneme CLI
mneme stats

# Check hooks
kimi
/hooks

# Check MCP registration
# (look for mneme-kimi-code / memory_* tools in your client)

# Check web server
curl http://localhost:37777/api/health
```

---

## MCP Server Setup

mneme-kimi-code exposes **15 memory tools** via MCP (Model Context Protocol) for Claude Desktop, Cursor, Goose, Kimi Code CLI, and other MCP-compatible clients.

### Claude Desktop

1. Open Claude Desktop → Settings → Developer → Edit Config
2. Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "uvx",
      "args": ["--from", "mneme-kimi-code", "mneme", "mcp"]
    }
  }
}
```

Or with `uv tool` (recommended — faster startup):

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "mneme",
      "args": ["mcp"]
    }
  }
}
```

3. Restart Claude Desktop
4. Look for 🔌 mneme-kimi-code icon in the toolbar

### Cursor

1. Open Cursor → Settings → MCP
2. Add server:

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "uvx",
      "args": ["--from", "mneme-kimi-code", "mneme", "mcp"]
    }
  }
}
```

Or use the Command Palette (`Ctrl+Shift+P`) → "MCP: Add Server"

### Goose

1. Run in terminal:

```bash
goose configure --mcp-server mneme-kimi-code
# Enter command: uvx --from mneme-kimi-code mneme mcp
```

Or edit `~/.config/goose/mcp.json`:

```json
{
  "mcpServers": {
    "mneme-kimi-code": {
      "command": "uvx",
      "args": ["--from", "mneme-kimi-code", "mneme", "mcp"]
    }
  }
}
```

### Available MCP Tools

| Tool | Purpose |
|------|-------------|
| `memory_search` | Full-text search (FTS5) |
| `memory_semantic_search` | Vector similarity search |
| `memory_recall` | Get full observation by ID |
| `memory_timeline` | Chronological context |
| `memory_stats` | Memory statistics |
| `memory_by_concept` | Filter by concept tag |
| `memory_by_file` | Find observations for a file |
| `memory_workflow` | How to use memory (guide) |
| `smart_search` | Tree-sitter AST symbol search |
| `smart_outline` | File structural outline |
| `smart_unfold` | Symbol body extraction |
| `memory_build_collection` | Create knowledge collection |
| `memory_list_collections` | List collections |
| `memory_export_collection` | Export as markdown/JSON |
| `memory_query_collection` | Semantic Q&A over collection |

### Progressive Disclosure Workflow

To minimize token usage, MCP clients should follow this 3-layer pattern:

```
Step 1: memory_search or memory_semantic_search
  → Get compact index with IDs

Step 2: memory_timeline
  → Get context around interesting results

Step 3: memory_recall
  → Fetch full details ONLY for selected IDs
  → ~10x token savings vs fetching everything
```

### Troubleshooting MCP

| Problem | Solution |
|---------|----------|
| "Command not found" | Ensure `uvx` or `mneme` is in PATH |
| Server won't start | Check `mneme stats` works in terminal |
| No tools showing | Restart the MCP client (Claude/Cursor/Goose) |
| Slow startup | Use `uv tool install` instead of `uvx` |

---

## Uninstall

```bash
# Remove hooks, MCP entry, and data
python scripts/uninstall.py

# Or keep data
python scripts/uninstall.py --keep-data
```

Or manually:

```bash
# Remove hooks from ~/.kimi-code/config.toml
# Remove MCP server from ~/.kimi-code/mcp.json
# Remove data
rm -rf ~/.kimi-code/mneme
```
