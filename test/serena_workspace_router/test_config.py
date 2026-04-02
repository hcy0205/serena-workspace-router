from pathlib import Path
from textwrap import dedent

import yaml

from serena_workspace_router.config import (
    detect_project_languages,
    get_project_codex_config_path,
    get_project_root_serena_config_path,
    inspect_codex_serena_binding,
    inspect_project_root_serena_config,
    setup_codex_serena_binding,
    setup_project_root_serena_config,
)


def _write_project_file(project_root: Path, text: str) -> Path:
    config_path = get_project_root_serena_config_path(project_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dedent(text).strip() + "\n", encoding="utf-8")
    return config_path


def _write_codex_config(project_root: Path, text: str) -> Path:
    config_path = get_project_codex_config_path(project_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(dedent(text).strip() + "\n", encoding="utf-8")
    return config_path


def test_official_languages_config_is_accepted(tmp_path: Path) -> None:
    project_root = tmp_path / "official-project"
    project_root.mkdir()
    _write_project_file(
        project_root,
        """
        project_name: official-project
        languages:
        - typescript
        - python
        ignored_paths: []
        excluded_tools: []
        included_optional_tools: []
        fixed_tools: []
        read_only: false
        ignore_all_files_in_gitignore: true
        initial_prompt: ""
        encoding: utf-8
        symbol_info_budget: 10
        """,
    )

    state = inspect_project_root_serena_config(project_root)
    assert state.status == "ok"
    assert state.languages == ["typescript", "python"]
    assert state.uses_legacy_single_language_field is False
    assert state.recommended_repair is False


def test_single_language_is_compatible_but_marked_for_upgrade(tmp_path: Path) -> None:
    project_root = tmp_path / "legacy-project"
    project_root.mkdir()
    _write_project_file(
        project_root,
        """
        project_name: legacy-project
        language: python
        """,
    )

    state = inspect_project_root_serena_config(project_root)
    assert state.status == "ok"
    assert state.languages == ["python"]
    assert state.uses_legacy_single_language_field is True
    assert state.recommended_repair is True


def test_setup_upgrades_single_language_to_languages(tmp_path: Path) -> None:
    project_root = tmp_path / "upgrade-project"
    project_root.mkdir()
    config_path = _write_project_file(
        project_root,
        """
        project_name: upgrade-project
        language: python
        ignored_paths: []
        excluded_tools: []
        included_optional_tools: []
        fixed_tools: []
        read_only: false
        ignore_all_files_in_gitignore: true
        initial_prompt: ""
        encoding: utf-8
        symbol_info_budget: 10
        """,
    )

    state = setup_project_root_serena_config(project_root)
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert state.status == "repaired"
    assert config_data["languages"] == ["python"]
    assert "language" not in config_data


def test_detect_project_languages_prefers_primary_and_secondary_code_languages(tmp_path: Path) -> None:
    project_root = tmp_path / "mixed-project"
    (project_root / "web_ui" / "src").mkdir(parents=True)
    (project_root / "python_bridge").mkdir(parents=True)
    (project_root / "docs").mkdir(parents=True)

    (project_root / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (project_root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (project_root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (project_root / "web_ui" / "src" / "App.tsx").write_text("export function App() { return null; }\n", encoding="utf-8")
    (project_root / "python_bridge" / "web_main.py").write_text("def _load_env_file_once():\n    return None\n", encoding="utf-8")
    (project_root / "docs" / "README.md").write_text("# docs\n", encoding="utf-8")
    (project_root / "config.yaml").write_text("name: demo\n", encoding="utf-8")

    detected = detect_project_languages(project_root)
    assert detected == ["typescript", "python"]


def test_detect_project_languages_prefers_nested_typescript_workspace_as_primary(tmp_path: Path) -> None:
    project_root = tmp_path / "workspace-project"
    (project_root / "web_ui" / "src").mkdir(parents=True)
    (project_root / "python_bridge").mkdir(parents=True)

    (project_root / "requirements.txt").write_text("fastapi==0.115.0\n", encoding="utf-8")
    (project_root / "web_ui" / "package.json").write_text('{"name":"demo-web"}', encoding="utf-8")
    (project_root / "web_ui" / "tsconfig.json").write_text("{}", encoding="utf-8")
    (project_root / "web_ui" / "src" / "App.tsx").write_text("export function App() { return null; }\n", encoding="utf-8")
    (project_root / "python_bridge" / "web_main.py").write_text("def main():\n    return None\n", encoding="utf-8")

    detected = detect_project_languages(project_root)
    assert detected == ["typescript", "python"]


def test_inspect_codex_serena_binding_reports_missing_when_project_and_global_are_unconfigured(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "no-binding-project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    state = inspect_codex_serena_binding(project_root, codex_home=codex_home)

    assert state.status == "missing"
    assert state.binding_scope == "none"
    assert state.project_config_present is False
    assert state.global_config_present is False
    assert state.requires_user_confirmation is True


def test_inspect_codex_serena_binding_flags_legacy_local_launcher(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "legacy-binding-project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    _write_codex_config(
        project_root,
        """
        [mcp_servers.serena]
        type = "stdio"
        command = "python"
        args = [".codex/start_serena_mcp.py"]
        startup_timeout_sec = 240
        """,
    )

    state = inspect_codex_serena_binding(project_root, codex_home=codex_home, include_global=False)

    assert state.status == "needs_repair"
    assert state.reason == "legacy-local-launcher"
    assert state.uses_legacy_local_launcher is True
    assert state.project_binding_present is True
    assert state.project_binding_ok is False


def test_inspect_codex_serena_binding_accepts_global_binding_when_project_binding_is_absent(tmp_path: Path, monkeypatch) -> None:
    bootstrap_project = tmp_path / "bootstrap-project"
    bootstrap_project.mkdir()
    target_project = tmp_path / "target-project"
    target_project.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    setup_codex_serena_binding(bootstrap_project, update_global=True, codex_home=codex_home)
    state = inspect_codex_serena_binding(target_project, codex_home=codex_home)

    assert state.status == "ok"
    assert state.binding_scope == "global"
    assert state.project_config_present is False
    assert state.global_binding_ok is True


def test_setup_codex_serena_binding_creates_project_binding(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "create-binding-project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    state = setup_codex_serena_binding(project_root, codex_home=codex_home)
    config_text = get_project_codex_config_path(project_root).read_text(encoding="utf-8")

    assert state.status == "created"
    assert state.project_binding_ok is True
    assert "--from" in config_text
    assert "serena-workspace-router" in config_text


def test_setup_codex_serena_binding_repairs_legacy_binding_and_creates_backup(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "repair-binding-project"
    project_root.mkdir()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    config_path = _write_codex_config(
        project_root,
        """
        [mcp_servers.serena]
        type = "stdio"
        command = "node"
        args = ["workspace-serena-mcp-launcher.mjs"]
        startup_timeout_sec = 60
        """,
    )

    state = setup_codex_serena_binding(project_root, codex_home=codex_home)
    repaired_text = config_path.read_text(encoding="utf-8")

    assert state.status == "repaired"
    assert state.project_binding_ok is True
    assert state.backup_paths
    assert "codex/workspace-router-v1.zip" in repaired_text
