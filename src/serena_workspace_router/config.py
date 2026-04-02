import hashlib
import os
import shutil
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
