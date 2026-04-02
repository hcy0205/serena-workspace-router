# ruff: noqa: RUF001
import hashlib
import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from serena.config.serena_config import (
    DEFAULT_PROJECT_SERENA_FOLDER_LOCATION,
    PROJECT_LOCAL_TEMPLATE_FILE,
    ProjectConfig,
    SerenaConfig,
)
from solidlsp.ls_config import Language

PROJECT_SERENA_DIRNAME = ".serena"
PROJECT_SERENA_CONFIG_FILENAME = "project.yml"
PROJECT_SERENA_LOCAL_FILENAME = "project.local.yml"
CODEX_CONFIG_DIRNAME = ".codex"
CODEX_CONFIG_FILENAME = "config.toml"
CODEX_START_SERENA_MCP_PY_FILENAME = "start_serena_mcp.py"
CODEX_START_SERENA_MCP_CMD_FILENAME = "start_serena_mcp.cmd"
DEFAULT_ROUTER_GITHUB_ZIP_URL = "https://github.com/hcy0205/serena-workspace-router/archive/refs/heads/codex/workspace-router-v1.zip"
DEFAULT_ROUTER_CONTEXT = "codex"
DEFAULT_ROUTER_STARTUP_TIMEOUT_SEC = 240
LEGACY_LOCAL_BINDING_MARKERS = (
    "workspace-serena-router",
    "workspace-serena-mcp-launcher.mjs",
    "run-serena-mcp-direct.py",
    "start_serena_mcp.py",
    "start_serena_mcp.cmd",
)
SERENA_TOML_TABLE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")
PROJECT_LANGUAGE_SCAN_MAX_FILES = 2_000
PROJECT_LANGUAGE_MARKER_SCAN_MAX_DEPTH = 2
AUTO_CONFIG_MAX_LANGUAGES = 2
AUTO_CONFIG_MIN_SECONDARY_SCORE = 3
AUTO_CONFIG_AUXILIARY_LANGUAGES = {"markdown", "toml", "yaml"}
PROJECT_LANGUAGE_ALIASES = {
    "c#": "csharp",
    "javascript": "typescript",
    "js": "typescript",
    "jsx": "typescript",
    "py": "python",
    "shell": "bash",
    "sh": "bash",
    "ts": "typescript",
    "tsx": "typescript",
}
PROJECT_LANGUAGE_MARKERS = {
    "typescript": ("package.json", "tsconfig.json", "jsconfig.json"),
    "python": ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
    "php": ("composer.json",),
    "ruby": ("Gemfile",),
    "swift": ("Package.swift",),
    "elixir": ("mix.exs",),
    "dart": ("pubspec.yaml",),
}
PROJECT_LANGUAGE_MARKER_PRIORITY = {language: len(PROJECT_LANGUAGE_MARKERS) - index for index, language in enumerate(PROJECT_LANGUAGE_MARKERS)}
PROJECT_LANGUAGE_EXTENSION_MAP = {
    ".c": "cpp",
    ".cc": "cpp",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".dart": "dart",
    ".erl": "erlang",
    ".ex": "elixir",
    ".exs": "elixir",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".go": "go",
    ".groovy": "groovy",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hs": "haskell",
    ".java": "java",
    ".jl": "julia",
    ".js": "typescript",
    ".jsx": "typescript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".lua": "lua",
    ".m": "matlab",
    ".md": "markdown",
    ".nix": "nix",
    ".pas": "pascal",
    ".php": "php",
    ".pl": "perl",
    ".ps1": "powershell",
    ".py": "python",
    ".r": "r",
    ".rb": "ruby",
    ".rego": "rego",
    ".rs": "rust",
    ".scala": "scala",
    ".sh": "bash",
    ".swift": "swift",
    ".terraform": "terraform",
    ".tf": "terraform",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zig": "zig",
}
PROJECT_LANGUAGE_SCAN_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".idea",
    ".next",
    ".nuxt",
    ".serena",
    ".svn",
    ".turbo",
    ".venv",
    "__pycache__",
    "bin",
    "build",
    "cache",
    "coverage",
    "dist",
    "node_modules",
    "obj",
    "out",
    "target",
    "tmp",
    "vendor",
    "venv",
}
SUPPORTED_PROJECT_LANGUAGES = {language.value for language in Language}


@dataclass(slots=True)
class ProjectSerenaConfigState:
    status: str
    reason: str
    project_root: str
    project_yml_path: str
    backup_path: str | None = None
    languages: list[str] = field(default_factory=list)
    project_name: str = ""
    ignored_paths: list[str] = field(default_factory=list)
    uses_legacy_single_language_field: bool = False
    recommended_repair: bool = False
    requires_user_confirmation: bool = False

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CodexSerenaBindingState:
    status: str
    reason: str
    project_root: str
    binding_scope: str
    project_codex_config_path: str
    global_codex_config_path: str
    project_start_script_py_path: str
    project_start_script_cmd_path: str
    expected_command: str
    expected_args: list[str] = field(default_factory=list)
    expected_env: dict[str, str] = field(default_factory=dict)
    binding_source_url: str = DEFAULT_ROUTER_GITHUB_ZIP_URL
    project_config_present: bool = False
    global_config_present: bool = False
    project_binding_present: bool = False
    global_binding_present: bool = False
    project_binding_ok: bool = False
    global_binding_ok: bool = False
    project_binding_reason: str = ""
    global_binding_reason: str = ""
    uses_github_zip_source: bool = False
    uses_git_source: bool = False
    uses_legacy_local_launcher: bool = False
    recommended_repair: bool = False
    requires_user_confirmation: bool = False
    backup_paths: list[str] = field(default_factory=list)
    updated_files: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class _CodexBindingTarget:
    command: str
    args: list[str]
    env: dict[str, str]
    startup_timeout_sec: int
    source_url: str


@dataclass(slots=True)
class _CodexConfigInspection:
    config_path: str
    config_present: bool
    binding_present: bool
    binding_ok: bool
    reason: str
    uses_github_zip_source: bool = False
    uses_git_source: bool = False
    uses_legacy_local_launcher: bool = False


def sanitize_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value))
    cleaned = cleaned.strip("-")
    return cleaned[:80] or "default"


def sha1_text(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()


def normalize_project_root(project_path: str | Path) -> Path:
    resolved = Path(project_path).expanduser().resolve()
    try:
        if resolved.is_dir():
            return resolved
    except OSError:
        return resolved
    return resolved.parent if resolved.suffix else resolved


def get_project_root_serena_config_path(project_path: str | Path) -> Path:
    return normalize_project_root(project_path) / PROJECT_SERENA_DIRNAME / PROJECT_SERENA_CONFIG_FILENAME


def get_project_root_serena_local_config_path(project_path: str | Path) -> Path:
    return normalize_project_root(project_path) / PROJECT_SERENA_DIRNAME / PROJECT_SERENA_LOCAL_FILENAME


def get_project_codex_config_path(project_path: str | Path) -> Path:
    return normalize_project_root(project_path) / CODEX_CONFIG_DIRNAME / CODEX_CONFIG_FILENAME


def get_project_start_serena_mcp_py_path(project_path: str | Path) -> Path:
    return normalize_project_root(project_path) / CODEX_CONFIG_DIRNAME / CODEX_START_SERENA_MCP_PY_FILENAME


def get_project_start_serena_mcp_cmd_path(project_path: str | Path) -> Path:
    return normalize_project_root(project_path) / CODEX_CONFIG_DIRNAME / CODEX_START_SERENA_MCP_CMD_FILENAME


def get_codex_home(codex_home: str | Path | None = None) -> Path:
    configured = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return configured.expanduser().resolve()


def get_global_codex_config_path(codex_home: str | Path | None = None) -> Path:
    return get_codex_home(codex_home) / CODEX_CONFIG_FILENAME


def _normalize_language_name(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().strip("\"'").lower()
    if not normalized:
        return None
    normalized = PROJECT_LANGUAGE_ALIASES.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_PROJECT_LANGUAGES else None


def _normalize_languages(values: list[object] | tuple[object, ...] | None) -> list[str]:
    languages: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        normalized = _normalize_language_name(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            languages.append(normalized)
    return languages


def _normalize_string_list(values: list[object] | tuple[object, ...] | None) -> list[str]:
    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            normalized_values.append(text)
    return normalized_values


def _load_project_yaml_data(project_yml_path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(project_yml_path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("`.serena/project.yml` 必须是 YAML 对象")
    return loaded


def _build_default_project_name(project_root: Path) -> str:
    return project_root.name or "project"


def _build_router_serena_config() -> SerenaConfig:
    serena_config = SerenaConfig.from_config_file()
    serena_config.project_serena_folder_location = DEFAULT_PROJECT_SERENA_FOLDER_LOCATION
    return serena_config


def _backup_file(path: Path, reason: str) -> Path:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.bak-{reason}-{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def _score_language(scores: dict[str, int], language: str, delta: int = 1) -> None:
    scores[language] = scores.get(language, 0) + delta


def _iter_marker_scan_dirs(project_root: Path) -> list[Path]:
    scan_dirs: list[Path] = []
    stack: list[tuple[Path, int]] = [(project_root, 0)]
    while stack:
        current_dir, depth = stack.pop()
        scan_dirs.append(current_dir)
        if depth >= PROJECT_LANGUAGE_MARKER_SCAN_MAX_DEPTH:
            continue
        try:
            entries = list(current_dir.iterdir())
        except OSError:
            continue
        child_dirs = [
            entry
            for entry in entries
            if entry.is_dir() and entry.name not in PROJECT_LANGUAGE_SCAN_IGNORED_DIRS
        ]
        stack.extend((child_dir, depth + 1) for child_dir in reversed(child_dirs))
    return scan_dirs


def _collect_marker_language_scores(
    project_root: Path,
    scores: dict[str, int],
    marker_languages: set[str],
    marker_counts: dict[str, int],
) -> None:
    scan_dirs = _iter_marker_scan_dirs(project_root)

    for language, filenames in PROJECT_LANGUAGE_MARKERS.items():
        hits = sum(1 for scan_dir in scan_dirs for filename in filenames if (scan_dir / filename).exists())
        if not hits:
            continue
        _score_language(scores, language, 3 + max(0, hits - 1))
        marker_languages.add(language)
        marker_counts[language] = hits

    csharp_hits = 0
    for scan_dir in scan_dirs:
        try:
            entries = list(scan_dir.iterdir())
        except OSError:
            continue
        csharp_hits += sum(1 for entry in entries if entry.is_file() and entry.suffix.lower() in {".csproj", ".sln"})
    if csharp_hits:
        _score_language(scores, "csharp", 3 + max(0, csharp_hits - 1))
        marker_languages.add("csharp")
        marker_counts["csharp"] = csharp_hits


def _collect_extension_language_scores(project_root: Path, scores: dict[str, int]) -> None:
    stack = [project_root]
    scanned_files = 0
    while stack and scanned_files < PROJECT_LANGUAGE_SCAN_MAX_FILES:
        current_dir = stack.pop()
        try:
            entries = list(current_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in PROJECT_LANGUAGE_SCAN_IGNORED_DIRS:
                    stack.append(entry)
                continue
            if not entry.is_file():
                continue
            scanned_files += 1
            language = PROJECT_LANGUAGE_EXTENSION_MAP.get(entry.suffix.lower())
            if language:
                _score_language(scores, language, 1)
            if scanned_files >= PROJECT_LANGUAGE_SCAN_MAX_FILES:
                break


def detect_project_languages(project_path: str | Path) -> list[str]:
    project_root = normalize_project_root(project_path)
    if not project_root.exists():
        return ["typescript"]

    scores: dict[str, int] = {}
    marker_languages: set[str] = set()
    marker_counts: dict[str, int] = {}
    _collect_marker_language_scores(project_root, scores, marker_languages, marker_counts)
    _collect_extension_language_scores(project_root, scores)

    sorted_languages = sorted(
        scores.items(),
        key=lambda item: (-item[1], -(PROJECT_LANGUAGE_MARKER_PRIORITY.get(item[0], 0)), item[0]),
    )
    if not sorted_languages:
        return ["typescript"]

    selected: list[str] = []
    for index, (language, score) in enumerate(sorted_languages):
        is_primary = index == 0
        is_auxiliary = language in AUTO_CONFIG_AUXILIARY_LANGUAGES
        should_include = is_primary or language in marker_languages or (not is_auxiliary and score >= AUTO_CONFIG_MIN_SECONDARY_SCORE)
        if not should_include:
            continue
        selected.append(language)
        if len(selected) >= AUTO_CONFIG_MAX_LANGUAGES:
            break

    if (
        "typescript" in selected
        and "python" in selected
        and marker_counts.get("typescript", 0) >= marker_counts.get("python", 0)
        and marker_counts.get("typescript", 0) > 0
    ):
        selected = ["typescript", *[language for language in selected if language != "typescript"]]

    return selected or [sorted_languages[0][0]]


def inspect_project_root_serena_config(project_path: str | Path) -> ProjectSerenaConfigState:
    project_root = normalize_project_root(project_path)
    project_yml_path = get_project_root_serena_config_path(project_root)
    project_name = _build_default_project_name(project_root)
    detected_languages = detect_project_languages(project_root)

    if not project_root.exists() or not project_root.is_dir():
        return ProjectSerenaConfigState(
            status="missing",
            reason="project-root-missing",
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            project_name=project_name,
            languages=detected_languages,
            requires_user_confirmation=True,
        )

    if not project_yml_path.exists():
        return ProjectSerenaConfigState(
            status="missing",
            reason="missing-project-config",
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            project_name=project_name,
            languages=detected_languages,
            requires_user_confirmation=True,
        )

    try:
        yaml_data = _load_project_yaml_data(project_yml_path)
    except Exception:
        return ProjectSerenaConfigState(
            status="needs_repair",
            reason="unreadable-project-config",
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            project_name=project_name,
            languages=detected_languages,
            requires_user_confirmation=True,
        )

    loaded_project_name = str(yaml_data.get("project_name") or project_name)
    languages_from_list = _normalize_languages(yaml_data.get("languages") if isinstance(yaml_data.get("languages"), list) else [])
    single_language = _normalize_language_name(yaml_data.get("language"))
    languages = [single_language] if single_language and not languages_from_list else languages_from_list
    ignored_paths = _normalize_string_list(yaml_data.get("ignored_paths") if isinstance(yaml_data.get("ignored_paths"), list) else [])

    if not languages:
        return ProjectSerenaConfigState(
            status="needs_repair",
            reason="invalid-project-config",
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            project_name=loaded_project_name,
            languages=detected_languages,
            requires_user_confirmation=True,
        )

    uses_legacy_single_language_field = bool(single_language and not languages_from_list)
    return ProjectSerenaConfigState(
        status="ok",
        reason="legacy-single-language-field" if uses_legacy_single_language_field else "existing-project-config-usable",
        project_root=str(project_root),
        project_yml_path=str(project_yml_path),
        project_name=loaded_project_name,
        languages=languages,
        ignored_paths=ignored_paths,
        uses_legacy_single_language_field=uses_legacy_single_language_field,
        recommended_repair=uses_legacy_single_language_field,
    )


def describe_project_root_serena_config_requirement(state: ProjectSerenaConfigState) -> str:
    reason_text_by_code = {
        "project-root-missing": "项目根目录不存在或无法访问",
        "missing-project-config": "缺少 `.serena/project.yml`",
        "legacy-single-language-field": "仍在使用兼容读法 `language:`，建议升级为官方 `languages:` 列表",
        "invalid-project-config": "`.serena/project.yml` 内容无效",
        "unreadable-project-config": "`.serena/project.yml` 无法读取",
    }
    reason_text = reason_text_by_code.get(state.reason, state.reason or "需要补齐 Serena 配置")
    return (
        f"项目 {state.project_root} 的 Serena 配置尚未就绪：{reason_text}。"
        "如需我自动创建或修复，请先征得用户同意，然后调用 `setup_project_serena`。"
    )


def _language_objects(language_names: list[str]) -> list[Language]:
    return [Language(language_name) for language_name in language_names]


def setup_project_root_serena_config(project_path: str | Path) -> ProjectSerenaConfigState:
    inspected = inspect_project_root_serena_config(project_path)
    project_root = Path(inspected.project_root)
    project_yml_path = Path(inspected.project_yml_path)
    project_yml_path.parent.mkdir(parents=True, exist_ok=True)
    serena_config = _build_router_serena_config()

    if inspected.status == "ok" and not inspected.uses_legacy_single_language_field:
        if not get_project_root_serena_local_config_path(project_root).exists():
            shutil.copy(PROJECT_LOCAL_TEMPLATE_FILE, get_project_root_serena_local_config_path(project_root))
        return inspected

    if inspected.status == "missing":
        project_config = ProjectConfig.autogenerate(
            project_root,
            serena_config=serena_config,
            project_name=inspected.project_name or _build_default_project_name(project_root),
            languages=_language_objects(inspected.languages or detect_project_languages(project_root)),
            save_to_disk=True,
            interactive=False,
        )
        if not get_project_root_serena_local_config_path(project_root).exists():
            shutil.copy(PROJECT_LOCAL_TEMPLATE_FILE, get_project_root_serena_local_config_path(project_root))
        return ProjectSerenaConfigState(
            status="created",
            reason=inspected.reason,
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            languages=[language.value for language in project_config.languages],
            project_name=project_config.project_name,
        )

    backup_path = _backup_file(project_yml_path, inspected.reason)
    if inspected.uses_legacy_single_language_field:
        project_config = ProjectConfig.load(project_root, serena_config=serena_config, autogenerate=False)
        project_config.save(str(project_yml_path))
        return ProjectSerenaConfigState(
            status="repaired",
            reason="legacy-single-language-field",
            project_root=str(project_root),
            project_yml_path=str(project_yml_path),
            backup_path=str(backup_path),
            languages=[language.value for language in project_config.languages],
            project_name=project_config.project_name,
        )

    project_config = ProjectConfig.autogenerate(
        project_root,
        serena_config=serena_config,
        project_name=inspected.project_name or _build_default_project_name(project_root),
        languages=_language_objects(inspected.languages or detect_project_languages(project_root)),
        save_to_disk=True,
        interactive=False,
    )
    if not get_project_root_serena_local_config_path(project_root).exists():
        shutil.copy(PROJECT_LOCAL_TEMPLATE_FILE, get_project_root_serena_local_config_path(project_root))
    return ProjectSerenaConfigState(
        status="repaired",
        reason=inspected.reason,
        project_root=str(project_root),
        project_yml_path=str(project_yml_path),
        backup_path=str(backup_path),
        languages=[language.value for language in project_config.languages],
        project_name=project_config.project_name,
    )


def _default_appdata_dir(codex_home: Path) -> Path:
    configured = os.environ.get("APPDATA")
    if configured:
        return Path(configured).expanduser().resolve()
    return (codex_home.parent / "AppData" / "Roaming").resolve()


def _default_uvx_command(codex_home: Path) -> str:
    executable_name = "uvx.exe" if os.name == "nt" else "uvx"
    return str(codex_home.parent / ".local" / "bin" / executable_name)


def _build_codex_binding_target(codex_home: str | Path | None = None) -> _CodexBindingTarget:
    codex_home_path = get_codex_home(codex_home)
    appdata_dir = _default_appdata_dir(codex_home_path)
    source_url = os.environ.get("SERENA_WORKSPACE_ROUTER_SOURCE_URL", DEFAULT_ROUTER_GITHUB_ZIP_URL)
    return _CodexBindingTarget(
        command=_default_uvx_command(codex_home_path),
        args=[
            "--from",
            source_url,
            "serena-workspace-router",
            "start-mcp-server",
            "--context",
            DEFAULT_ROUTER_CONTEXT,
            "--enable-web-dashboard",
            "false",
            "--enable-gui-log-window",
            "false",
        ],
        env={
            "WORKSPACE_SERENA_RUNTIME_DIR": str(appdata_dir / "MCP Router" / "serena-runtime"),
            "WORKSPACE_SERENA_SHARED_LANGUAGE_SERVERS_DIR": str(codex_home_path.parent / ".serena" / "language_servers"),
            "PROJECT_SHARED_COORDINATION_DIR": str(appdata_dir / "MCP Router" / "project-shared-space"),
            "LAUNCHER_DEFAULT_CLIENT_ID": DEFAULT_ROUTER_CONTEXT,
        },
        startup_timeout_sec=DEFAULT_ROUTER_STARTUP_TIMEOUT_SEC,
        source_url=source_url,
    )


def _normalize_pathish_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return text.replace("\\", "/").rstrip("/").casefold()


def _command_matches_expected(command: object, expected_command: str) -> bool:
    actual = _normalize_pathish_text(command)
    expected = _normalize_pathish_text(expected_command)
    if actual == expected:
        return True
    return Path(str(command or "")).name.casefold() in {"uvx", "uvx.exe"} and Path(expected_command).name.casefold() in {"uvx", "uvx.exe"}


def _list_of_strings_from_object(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _extract_source_from_args(args: list[str]) -> str:
    for index, item in enumerate(args):
        if item == "--from" and index + 1 < len(args):
            return args[index + 1]
    return ""


def _env_contains_expected(actual_env: Mapping[str, object], expected_env: Mapping[str, str]) -> bool:
    for key, expected_value in expected_env.items():
        actual_value = actual_env.get(key)
        if key.endswith("_DIR"):
            if _normalize_pathish_text(actual_value) != _normalize_pathish_text(expected_value):
                return False
            continue
        if str(actual_value or "").strip() != str(expected_value):
            return False
    return True


def _contains_serena_binding_text(config_text: str) -> bool:
    normalized = config_text.casefold()
    return "[mcp_servers.serena]" in normalized or "[mcp_servers.serena.env]" in normalized


def _inspect_codex_config_file(config_path: Path, target: _CodexBindingTarget) -> _CodexConfigInspection:
    if not config_path.exists():
        return _CodexConfigInspection(
            config_path=str(config_path),
            config_present=False,
            binding_present=False,
            binding_ok=False,
            reason="missing-codex-config",
        )

    config_text = config_path.read_text(encoding="utf-8")
    try:
        config_data = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError:
        return _CodexConfigInspection(
            config_path=str(config_path),
            config_present=True,
            binding_present=_contains_serena_binding_text(config_text),
            binding_ok=False,
            reason="unreadable-codex-config",
            uses_legacy_local_launcher=any(marker in config_text.casefold() for marker in LEGACY_LOCAL_BINDING_MARKERS),
            uses_git_source="git+https://github.com/" in config_text.casefold(),
            uses_github_zip_source=target.source_url.casefold() in config_text.casefold(),
        )

    mcp_servers = config_data.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return _CodexConfigInspection(
            config_path=str(config_path),
            config_present=True,
            binding_present=False,
            binding_ok=False,
            reason="missing-serena-binding",
        )

    binding = mcp_servers.get("serena")
    if not isinstance(binding, dict):
        return _CodexConfigInspection(
            config_path=str(config_path),
            config_present=True,
            binding_present=False,
            binding_ok=False,
            reason="missing-serena-binding",
        )

    command = str(binding.get("command") or "")
    args = _list_of_strings_from_object(binding.get("args"))
    env = binding.get("env") if isinstance(binding.get("env"), dict) else {}
    source_url = _extract_source_from_args(args)
    combined_binding_text = " ".join([command, *args]).casefold()
    uses_github_zip_source = _normalize_pathish_text(source_url) == _normalize_pathish_text(target.source_url)
    uses_git_source = source_url.casefold().startswith("git+https://github.com/")
    uses_legacy_local_launcher = any(marker in combined_binding_text for marker in LEGACY_LOCAL_BINDING_MARKERS)

    binding_ok = (
        str(binding.get("type") or "") == "stdio"
        and _command_matches_expected(command, target.command)
        and args == target.args
        and int(binding.get("startup_timeout_sec") or 0) == target.startup_timeout_sec
        and _env_contains_expected(env, target.env)
    )
    if binding_ok:
        return _CodexConfigInspection(
            config_path=str(config_path),
            config_present=True,
            binding_present=True,
            binding_ok=True,
            reason="serena-binding-usable",
            uses_github_zip_source=uses_github_zip_source,
            uses_git_source=uses_git_source,
            uses_legacy_local_launcher=uses_legacy_local_launcher,
        )

    if uses_legacy_local_launcher:
        reason = "legacy-local-launcher"
    elif uses_git_source:
        reason = "git-source-binding"
    elif not uses_github_zip_source:
        reason = "nonstandard-serena-binding"
    elif not _env_contains_expected(env, target.env):
        reason = "missing-router-env"
    elif int(binding.get("startup_timeout_sec") or 0) != target.startup_timeout_sec:
        reason = "startup-timeout-mismatch"
    elif not _command_matches_expected(command, target.command):
        reason = "nonstandard-command"
    else:
        reason = "nonstandard-serena-binding"

    return _CodexConfigInspection(
        config_path=str(config_path),
        config_present=True,
        binding_present=True,
        binding_ok=False,
        reason=reason,
        uses_github_zip_source=uses_github_zip_source,
        uses_git_source=uses_git_source,
        uses_legacy_local_launcher=uses_legacy_local_launcher,
    )


def _resolve_binding_status(
    *,
    project_inspection: _CodexConfigInspection,
    global_inspection: _CodexConfigInspection,
) -> tuple[str, str, str]:
    if project_inspection.binding_ok:
        return "ok", "project-binding-usable", "project"
    if project_inspection.config_present and project_inspection.reason == "unreadable-codex-config":
        return "needs_repair", "invalid-project-codex-config", "project"
    if project_inspection.binding_present:
        return "needs_repair", project_inspection.reason, "project"
    if global_inspection.binding_ok:
        return "ok", "global-binding-usable", "global"
    if global_inspection.config_present and global_inspection.reason == "unreadable-codex-config":
        return "needs_repair", "invalid-global-codex-config", "global"
    if global_inspection.binding_present:
        return "needs_repair", global_inspection.reason, "global"
    if not project_inspection.config_present and not global_inspection.config_present:
        return "missing", "missing-codex-config", "none"
    return "missing", "missing-serena-binding", "none"


def inspect_codex_serena_binding(
    project_path: str | Path,
    *,
    include_global: bool = True,
    codex_home: str | Path | None = None,
) -> CodexSerenaBindingState:
    project_root = normalize_project_root(project_path)
    codex_home_path = get_codex_home(codex_home)
    project_config_path = get_project_codex_config_path(project_root)
    global_config_path = get_global_codex_config_path(codex_home_path)
    target = _build_codex_binding_target(codex_home_path)

    if not project_root.exists() or not project_root.is_dir():
        return CodexSerenaBindingState(
            status="missing",
            reason="project-root-missing",
            project_root=str(project_root),
            binding_scope="none",
            project_codex_config_path=str(project_config_path),
            global_codex_config_path=str(global_config_path),
            project_start_script_py_path=str(get_project_start_serena_mcp_py_path(project_root)),
            project_start_script_cmd_path=str(get_project_start_serena_mcp_cmd_path(project_root)),
            expected_command=target.command,
            expected_args=list(target.args),
            expected_env=dict(target.env),
            binding_source_url=target.source_url,
            requires_user_confirmation=True,
        )

    project_inspection = _inspect_codex_config_file(project_config_path, target)
    global_inspection = (
        _inspect_codex_config_file(global_config_path, target)
        if include_global
        else _CodexConfigInspection(
            config_path=str(global_config_path),
            config_present=False,
            binding_present=False,
            binding_ok=False,
            reason="global-inspection-disabled",
        )
    )
    status, reason, binding_scope = _resolve_binding_status(
        project_inspection=project_inspection,
        global_inspection=global_inspection,
    )
    return CodexSerenaBindingState(
        status=status,
        reason=reason,
        project_root=str(project_root),
        binding_scope=binding_scope,
        project_codex_config_path=str(project_config_path),
        global_codex_config_path=str(global_config_path),
        project_start_script_py_path=str(get_project_start_serena_mcp_py_path(project_root)),
        project_start_script_cmd_path=str(get_project_start_serena_mcp_cmd_path(project_root)),
        expected_command=target.command,
        expected_args=list(target.args),
        expected_env=dict(target.env),
        binding_source_url=target.source_url,
        project_config_present=project_inspection.config_present,
        global_config_present=global_inspection.config_present,
        project_binding_present=project_inspection.binding_present,
        global_binding_present=global_inspection.binding_present,
        project_binding_ok=project_inspection.binding_ok,
        global_binding_ok=global_inspection.binding_ok,
        project_binding_reason=project_inspection.reason,
        global_binding_reason=global_inspection.reason,
        uses_github_zip_source=project_inspection.uses_github_zip_source or global_inspection.uses_github_zip_source,
        uses_git_source=project_inspection.uses_git_source or global_inspection.uses_git_source,
        uses_legacy_local_launcher=project_inspection.uses_legacy_local_launcher or global_inspection.uses_legacy_local_launcher,
        recommended_repair=status != "ok",
        requires_user_confirmation=status in {"missing", "needs_repair"},
    )


def _toml_literal_string(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_toml_string(value: str) -> str:
    return _toml_literal_string(value) if ("\\" in value or "/" in value or ":" in value[:3]) else _toml_string(value)


def _render_toml_string_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _render_serena_binding_block(target: _CodexBindingTarget) -> str:
    lines = [
        "[mcp_servers.serena]",
        'type = "stdio"',
        f"command = {_render_toml_string(target.command)}",
        f"args = {_render_toml_string_array(target.args)}",
        f"startup_timeout_sec = {target.startup_timeout_sec}",
        "",
        "[mcp_servers.serena.env]",
        f"WORKSPACE_SERENA_RUNTIME_DIR = {_render_toml_string(target.env['WORKSPACE_SERENA_RUNTIME_DIR'])}",
        f"WORKSPACE_SERENA_SHARED_LANGUAGE_SERVERS_DIR = {_render_toml_string(target.env['WORKSPACE_SERENA_SHARED_LANGUAGE_SERVERS_DIR'])}",
        f"PROJECT_SHARED_COORDINATION_DIR = {_render_toml_string(target.env['PROJECT_SHARED_COORDINATION_DIR'])}",
        f'LAUNCHER_DEFAULT_CLIENT_ID = "{target.env["LAUNCHER_DEFAULT_CLIENT_ID"]}"',
        "",
    ]
    return "\n".join(lines)


def _find_serena_table_ranges(config_text: str) -> list[tuple[int, int]]:
    lines = config_text.splitlines(keepends=True)
    headers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = SERENA_TOML_TABLE_RE.match(line)
        if match:
            headers.append((index, match.group(1).strip()))
    ranges: list[tuple[int, int]] = []
    for index, (start, name) in enumerate(headers):
        if name not in {"mcp_servers.serena", "mcp_servers.serena.env"}:
            continue
        end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
        ranges.append((start, end))
    return ranges


def _upsert_serena_binding_text(existing_text: str, replacement_block: str) -> str:
    ranges = _find_serena_table_ranges(existing_text)
    if not ranges:
        base = existing_text.rstrip()
        return f"{base}\n\n{replacement_block}" if base else replacement_block

    lines = existing_text.splitlines(keepends=True)
    insert_at = min(start for start, _ in ranges)
    for start, end in sorted(ranges, reverse=True):
        del lines[start:end]
    replacement_lines = replacement_block.splitlines(keepends=True)
    lines[insert_at:insert_at] = replacement_lines
    text = "".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


def _write_codex_binding_file(
    config_path: Path,
    *,
    target: _CodexBindingTarget,
    backup_paths: list[str],
    updated_files: list[str],
) -> bool:
    replacement_block = _render_serena_binding_block(target)
    existed_before = config_path.exists()
    existing_text = config_path.read_text(encoding="utf-8") if existed_before else ""
    if existed_before:
        try:
            tomllib.loads(existing_text)
            rendered_text = _upsert_serena_binding_text(existing_text, replacement_block)
        except tomllib.TOMLDecodeError:
            rendered_text = replacement_block
    else:
        rendered_text = replacement_block

    if existing_text.replace("\r\n", "\n") == rendered_text.replace("\r\n", "\n"):
        return existed_before

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if existed_before:
        backup_path = _backup_file(config_path, "serena-binding")
        backup_paths.append(str(backup_path))
    config_path.write_text(rendered_text, encoding="utf-8")
    updated_files.append(str(config_path))
    return existed_before


def setup_codex_serena_binding(
    project_path: str | Path,
    *,
    update_global: bool = False,
    codex_home: str | Path | None = None,
) -> CodexSerenaBindingState:
    target = _build_codex_binding_target(codex_home)
    project_root = normalize_project_root(project_path)
    if not project_root.exists() or not project_root.is_dir():
        return inspect_codex_serena_binding(project_root, include_global=True, codex_home=codex_home)
    project_config_path = get_project_codex_config_path(project_root)
    global_config_path = get_global_codex_config_path(codex_home)
    backup_paths: list[str] = []
    updated_files: list[str] = []

    project_existed = _write_codex_binding_file(
        project_config_path,
        target=target,
        backup_paths=backup_paths,
        updated_files=updated_files,
    )
    global_existed = False
    if update_global:
        global_existed = _write_codex_binding_file(
            global_config_path,
            target=target,
            backup_paths=backup_paths,
            updated_files=updated_files,
        )

    inspected = inspect_codex_serena_binding(project_root, include_global=True, codex_home=codex_home)
    if not updated_files:
        status = "ok"
        reason = "project-binding-already-usable"
    elif any(Path(path) == project_config_path and project_existed for path in updated_files) or (
        update_global and any(Path(path) == global_config_path and global_existed for path in updated_files)
    ):
        status = "repaired"
        reason = "codex-binding-updated"
    else:
        status = "created"
        reason = "codex-binding-created"

    return CodexSerenaBindingState(
        status=status,
        reason=reason,
        project_root=inspected.project_root,
        binding_scope=inspected.binding_scope,
        project_codex_config_path=inspected.project_codex_config_path,
        global_codex_config_path=inspected.global_codex_config_path,
        project_start_script_py_path=inspected.project_start_script_py_path,
        project_start_script_cmd_path=inspected.project_start_script_cmd_path,
        expected_command=inspected.expected_command,
        expected_args=inspected.expected_args,
        expected_env=inspected.expected_env,
        binding_source_url=inspected.binding_source_url,
        project_config_present=inspected.project_config_present,
        global_config_present=inspected.global_config_present,
        project_binding_present=inspected.project_binding_present,
        global_binding_present=inspected.global_binding_present,
        project_binding_ok=inspected.project_binding_ok,
        global_binding_ok=inspected.global_binding_ok,
        project_binding_reason=inspected.project_binding_reason,
        global_binding_reason=inspected.global_binding_reason,
        uses_github_zip_source=inspected.uses_github_zip_source,
        uses_git_source=inspected.uses_git_source,
        uses_legacy_local_launcher=inspected.uses_legacy_local_launcher,
        recommended_repair=False,
        requires_user_confirmation=False,
        backup_paths=backup_paths,
        updated_files=updated_files,
    )
