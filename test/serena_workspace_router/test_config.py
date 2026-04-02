from pathlib import Path
from textwrap import dedent

import yaml

from serena_workspace_router.config import (
    detect_project_languages,
    get_project_root_serena_config_path,
    inspect_project_root_serena_config,
    setup_project_root_serena_config,
)


def _write_project_file(project_root: Path, text: str) -> Path:
    config_path = get_project_root_serena_config_path(project_root)
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
