"""Tests for mneme.core.installer host-integration steps.

Focus on config-MERGE correctness: the register_* steps must extend the user's
existing Kimi/Claude config without clobbering unrelated entries, and must be
idempotent. That merge behavior is the part of the install path that
historically broke, and it was previously untested (it lived inline in cli.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mneme import __version__
from mneme.core import installer


def test_build_plugin_json_has_expected_tools_and_version() -> None:
    manifest = installer.build_plugin_json()

    assert manifest["name"] == "mneme-kimi-code"
    assert manifest["version"] == __version__
    tool_names = {t["name"] for t in manifest["tools"]}
    assert tool_names == {"mneme_search", "mneme_timeline", "mneme_get"}
    for tool in manifest["tools"]:
        assert tool["command"][0] == "mneme"


def test_generate_plugin_json_writes_file(tmp_path: Path) -> None:
    installer.generate_plugin_json(tmp_path)

    data = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8"))
    assert data["name"] == "mneme-kimi-code"


def test_create_default_config_writes_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "get_mneme_dir", lambda: tmp_path)

    assert installer.create_default_config() is True
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["server"]["port"] == 37777

    # Second run must not fail and must not overwrite an existing config.
    cfg_path.write_text(json.dumps({"custom": True}), encoding="utf-8")
    assert installer.create_default_config() is True
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"custom": True}


def test_register_mcp_preserves_other_servers_and_drops_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "get_kimi_dir", lambda: tmp_path)
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}, "kimi-mneme": {"command": "old"}}}),
        encoding="utf-8",
    )

    assert installer.register_mcp() is True

    servers = json.loads(mcp_path.read_text(encoding="utf-8"))["mcpServers"]
    assert "other" in servers  # foreign server preserved
    assert "kimi-mneme" not in servers  # legacy key dropped
    assert servers["mneme-kimi-code"]["args"] == ["-m", "mneme.mcp_server"]


def test_register_mcp_creates_file_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer, "get_kimi_dir", lambda: tmp_path)

    assert installer.register_mcp() is True

    data = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert "mneme-kimi-code" in data["mcpServers"]


def test_register_mcp_claude_adds_mneme_and_preserves_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {"foreign": {"type": "stdio"}, "mneme-kimi-code": {"old": 1}},
                "otherKey": 5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer, "_claude_json_path", lambda: claude_json)

    assert installer.register_mcp_claude() is True

    data = json.loads(claude_json.read_text(encoding="utf-8"))
    assert data["otherKey"] == 5  # unrelated top-level key preserved
    servers = data["mcpServers"]
    assert "foreign" in servers  # foreign server preserved
    assert "mneme-kimi-code" not in servers  # legacy Kimi key dropped
    assert servers["mneme"]["env"] == {"MNEME_TARGET": "claude"}


def test_register_hooks_claude_preserves_foreign_hooks_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_project_root -> empty dir, so copy_hook_scripts finds no scripts to copy;
    # we only exercise the settings.json merge here.
    monkeypatch.setattr(installer, "get_project_root", lambda: tmp_path)
    fake_target = SimpleNamespace(config_dir=tmp_path, data_dir=tmp_path)
    monkeypatch.setattr(installer, "claude_target", lambda: fake_target)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": "echo foreign"}]}]
                }
            }
        ),
        encoding="utf-8",
    )

    assert installer.register_hooks_claude() is True

    groups = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    commands = [h["command"] for g in groups for h in g["hooks"]]
    assert any("echo foreign" in c for c in commands)  # foreign hook preserved
    assert any("session_start.py" in c for c in commands)  # mneme hook added

    # Idempotent: a second run must not duplicate the mneme hook group.
    assert installer.register_hooks_claude() is True
    groups2 = json.loads(settings_path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    mneme_hooks = [h for g in groups2 for h in g["hooks"] if "session_start.py" in h["command"]]
    assert len(mneme_hooks) == 1
