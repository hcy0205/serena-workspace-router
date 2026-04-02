# ruff: noqa: RUF001
import asyncio
import json
import os
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

from serena_workspace_router.config import (
    ProjectSerenaConfigState,
    describe_project_root_serena_config_requirement,
    get_project_root_serena_config_path,
    get_project_root_serena_local_config_path,
    inspect_codex_serena_binding,
    inspect_project_root_serena_config,
    normalize_project_root,
    sanitize_segment,
    setup_codex_serena_binding,
    setup_project_root_serena_config,
    sha1_text,
)
from serena_workspace_router.coordination import ProjectSharedCoordinator

ROUTER_NAME = "serena-workspace-router"
ROUTER_VERSION = "0.1.0"
COORDINATOR_TOOL_NAMES = {
    "claim_scope",
    "release_scope",
    "publish_change",
    "read_project_feed",
    "read_active_claims",
}
LAUNCHER_TOOL_NAMES = {
    "inspect_project_serena",
    "setup_project_serena",
    "inspect_codex_serena_binding",
    "setup_codex_serena_binding",
}
BOOTSTRAP_TOOL_NAMES = {"initial_instructions", "get_current_config", "open_dashboard", "switch_modes"}
SHARED_MEMORY_MUTATION_TOOL_NAMES = {"write_memory", "edit_memory", "delete_memory", "rename_memory"}


@dataclass(slots=True)
class RouterConfig:
    runtime_base_dir: Path
    coordination_base_dir: Path
    child_command: str
    child_args: list[str]
    child_cwd: Path | None = None
    shared_language_servers_dir: Path | None = None
    default_client_id: str | None = "codex"
    default_workspace_id: str | None = None
    allow_session_fallback: bool = False
    bootstrap_project_path: Path | None = None
    stdio_session_id: str | None = None

    @classmethod
    def from_environment(cls, *, child_args: list[str]) -> "RouterConfig":
        runtime_base_dir = Path(os.environ.get("WORKSPACE_SERENA_RUNTIME_DIR", ".workspace-serena-runtime")).expanduser().resolve()
        coordination_base_dir = Path(
            os.environ.get("PROJECT_SHARED_COORDINATION_DIR", runtime_base_dir.parent / "project-shared-space")
        ).expanduser().resolve()
        shared_language_servers_dir = os.environ.get("WORKSPACE_SERENA_SHARED_LANGUAGE_SERVERS_DIR")
        bootstrap_project_path = os.environ.get("LAUNCHER_BOOTSTRAP_PROJECT_PATH")
        return cls(
            runtime_base_dir=runtime_base_dir,
            coordination_base_dir=coordination_base_dir,
            child_command=sys.executable,
            child_args=child_args,
            child_cwd=Path.cwd(),
            shared_language_servers_dir=Path(shared_language_servers_dir).expanduser().resolve() if shared_language_servers_dir else None,
            default_client_id=os.environ.get("LAUNCHER_DEFAULT_CLIENT_ID", "codex"),
            default_workspace_id=os.environ.get("LAUNCHER_DEFAULT_WORKSPACE_ID") or None,
            allow_session_fallback=os.environ.get("LAUNCHER_ALLOW_SESSION_FALLBACK", "").lower() in {"1", "true", "yes", "on"},
            bootstrap_project_path=Path(bootstrap_project_path).expanduser().resolve()
            if bootstrap_project_path
            else runtime_base_dir,
            stdio_session_id=os.environ.get("LAUNCHER_STDIO_SESSION_ID"),
        )


@dataclass(slots=True)
class ResolvedRequestContext:
    session_id: str
    workspace_id: str
    client_id: str
    project_path: str


@dataclass(slots=True)
class StoredWorkspaceContext:
    workspace_id: str
    client_id: str
    project_path: str


@dataclass(slots=True)
class InstancePlan:
    cache_key: str
    workspace_key: str
    project_key: str
    workspace_root: Path
    runtime_root: Path
    serena_home: Path
    config_path: Path
    project_store_root: Path
    project_serena_folder: Path
    log_path: Path
    child_command: str
    child_args: list[str]
    child_env: dict[str, str]
    child_cwd: Path | None


def _tool_input_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _json_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))],
        structuredContent=payload,
        isError=False,
    )


def _build_tool(name: str, description: str, input_schema: dict[str, Any]) -> Tool:
    write_tools = {"claim_scope", "release_scope", "publish_change", "setup_project_serena", "setup_codex_serena_binding"}
    destructive_tools = {"release_scope", "setup_project_serena", "setup_codex_serena_binding"}
    return Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        annotations=ToolAnnotations(
            title=" ".join(word.capitalize() for word in name.split("_")),
            readOnlyHint=name not in write_tools,
            destructiveHint=name in destructive_tools,
        ),
    )


LAUNCHER_TOOLS = [
    _build_tool(
        "inspect_project_serena",
        "检查当前或指定项目的 Serena 项目配置状态，不会创建或修改任何项目文件。",
        _tool_input_schema({"project_path": {"type": "string"}}),
    ),
    _build_tool(
        "setup_project_serena",
        "在用户明确同意后，为当前或指定项目创建或修复 `.serena/project.yml`。",
        _tool_input_schema({"project_path": {"type": "string"}}),
    ),
    _build_tool(
        "inspect_codex_serena_binding",
        "检查当前或指定项目的 Codex Serena 绑定是否已按 GitHub zip 形式配置。",
        _tool_input_schema(
            {
                "project_path": {"type": "string"},
                "include_global": {"type": "boolean"},
            }
        ),
    ),
    _build_tool(
        "setup_codex_serena_binding",
        "在用户明确同意后，为当前或指定项目创建或修复 `.codex/config.toml` 中的 Serena GitHub zip 绑定。",
        _tool_input_schema(
            {
                "project_path": {"type": "string"},
                "update_global": {"type": "boolean"},
            }
        ),
    ),
]

COORDINATOR_TOOLS = [
    _build_tool(
        "claim_scope",
        "声明当前 agent 正在处理的模块、文件与符号范围，返回冲突情况。",
        _tool_input_schema(
            {
                "module_name": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
                "ttl_ms": {"type": "number"},
            }
        ),
    ),
    _build_tool(
        "release_scope",
        "释放之前声明的占用范围。",
        _tool_input_schema({"claim_id": {"type": "number"}}, required=["claim_id"]),
    ),
    _build_tool(
        "publish_change",
        "发布本次改动摘要、涉及文件/符号与兼容说明，写入项目共享变更流。",
        _tool_input_schema(
            {
                "module_name": {"type": "string"},
                "summary": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
                "compatibility_notes": {"type": "string"},
                "base_commit": {"type": "string"},
            },
            required=["summary"],
        ),
    ),
    _build_tool(
        "read_project_feed",
        "读取项目近期变更流。",
        _tool_input_schema({"limit": {"type": "number"}}),
    ),
    _build_tool(
        "read_active_claims",
        "读取当前项目里仍然活跃的占用声明。",
        _tool_input_schema({}),
    ),
]


class SerenaChildInstance:
    def __init__(self, plan: InstancePlan) -> None:
        self.plan = plan
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._stderr_handle = None
        self._start_lock = asyncio.Lock()
        self._activation_lock = asyncio.Lock()
        self.active_project_path: str | None = None

    async def start(self) -> "SerenaChildInstance":
        if self._session is not None:
            return self
        async with self._start_lock:
            if self._session is not None:
                return self
            self.plan.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self.plan.log_path.open("a", encoding="utf-8")
            stack = AsyncExitStack()
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=self.plan.child_command,
                        args=self.plan.child_args,
                        env=self.plan.child_env,
                        cwd=self.plan.child_cwd,
                    ),
                    errlog=self._stderr_handle,
                )
            )
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
            self._stack = stack
            self._session = session
        return self

    async def list_tools(self) -> list[Tool]:
        await self.start()
        assert self._session is not None
        return (await self._session.list_tools()).tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, meta: dict[str, Any] | None = None) -> types.CallToolResult:
        await self.start()
        assert self._session is not None
        return await self._session.call_tool(name, arguments or {}, meta=meta)

    async def ensure_activated(self, project_path: str, meta: dict[str, Any] | None = None) -> None:
        if self.active_project_path == project_path:
            return
        async with self._activation_lock:
            if self.active_project_path == project_path:
                return
            result = await self.call_tool("activate_project", {"project": project_path}, meta=meta)
            if result.isError:
                raise RuntimeError(_tool_result_to_text(result) or f"无法激活项目 {project_path}")
            self.active_project_path = project_path

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseException:
                pass
            finally:
                self._stack = None
                self._session = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None


class WorkspaceSerenaRegistry:
    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self._instances: dict[str, SerenaChildInstance] = {}

    def get_or_create(self, context: ResolvedRequestContext) -> SerenaChildInstance:
        plan = self._materialize_instance_plan(context)
        if plan.cache_key not in self._instances:
            self._instances[plan.cache_key] = SerenaChildInstance(plan)
        return self._instances[plan.cache_key]

    async def close(self) -> None:
        for instance in self._instances.values():
            try:
                await instance.close()
            except Exception:
                continue

    def _materialize_instance_plan(self, context: ResolvedRequestContext) -> InstancePlan:
        project_root = normalize_project_root(context.project_path)
        workspace_key = f"{sanitize_segment(context.client_id)}--{sanitize_segment(context.workspace_id)}"
        project_hash = sha1_text(str(project_root))
        project_key = f"{sanitize_segment(project_root.name)}--{project_hash[:10]}"
        cache_key = f"{sanitize_segment(context.client_id)}::{sanitize_segment(context.workspace_id)}::{project_hash}"
        workspace_root = self.config.runtime_base_dir / workspace_key
        runtime_root = workspace_root / "instances" / project_key
        serena_home = runtime_root / "serena-home"
        config_path = serena_home / "serena_config.yml"
        project_store_root = workspace_root / "projects"
        project_serena_folder = project_store_root / project_key / ".serena"
        log_path = runtime_root / "logs" / "launcher.log"
        serena_home.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        project_serena_folder.mkdir(parents=True, exist_ok=True)
        (project_serena_folder / "memories").mkdir(parents=True, exist_ok=True)
        self._ensure_shared_language_servers(serena_home)
        config_path.write_text(self._render_isolated_serena_config(project_serena_folder), encoding="utf-8")
        child_env = {
            **os.environ,
            "SERENA_HOME": str(serena_home),
            "MCPR_WORKSPACE_ID": context.workspace_id,
            "MCPR_CLIENT_ID": context.client_id,
            "MCPR_PROJECT_PATH": str(project_root),
        }
        return InstancePlan(
            cache_key=cache_key,
            workspace_key=workspace_key,
            project_key=project_key,
            workspace_root=workspace_root,
            runtime_root=runtime_root,
            serena_home=serena_home,
            config_path=config_path,
            project_store_root=project_store_root,
            project_serena_folder=project_serena_folder,
            log_path=log_path,
            child_command=self.config.child_command,
            child_args=list(self.config.child_args),
            child_env=child_env,
            child_cwd=self.config.child_cwd,
        )

    def _ensure_shared_language_servers(self, serena_home: Path) -> None:
        source = self.config.shared_language_servers_dir
        if source is None or not source.is_dir():
            return
        target = serena_home / "language_servers"
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(source, target, target_is_directory=True)
        except OSError:
            shutil.copytree(source, target)

    @staticmethod
    def _render_isolated_serena_config(project_serena_folder: Path) -> str:
        project_serena_folder_text = str(project_serena_folder).replace("\\", "/")
        return "\n".join(
            [
                "# 自动生成：工作区隔离 Serena 配置",
                "language_backend: LSP",
                "gui_log_window: false",
                "web_dashboard: false",
                "web_dashboard_listen_address: 127.0.0.1",
                "web_dashboard_open_on_launch: false",
                "jetbrains_plugin_server_address: 127.0.0.1",
                "log_level: 20",
                "trace_lsp_communication: false",
                "ls_specific_settings: {}",
                "tool_timeout: 240",
                "excluded_tools: []",
                "included_optional_tools: []",
                "fixed_tools: []",
                "base_modes:",
                "default_modes:",
                "- interactive",
                "- editing",
                "default_max_tool_answer_chars: 150000",
                "token_count_estimator: CHAR_COUNT",
                "projects: []",
                "ignored_paths: []",
                "symbol_info_budget: 10.0",
                f'project_serena_folder_location: "{project_serena_folder_text}"',
                "read_only_memory_patterns: []",
                "line_ending: native",
                "",
            ]
        )


class WorkspaceSerenaRouterServer:
    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.registry = WorkspaceSerenaRegistry(config)
        self.coordinator = ProjectSharedCoordinator(config.coordination_base_dir)
        self.server: Server[Any, Any] = Server(
            ROUTER_NAME,
            version=ROUTER_VERSION,
            instructions="官方 Serena 的多工作区路由层，要求通过 _meta 传递 workspaceId、clientId、projectPath。",
        )
        self._workspace_context_by_session: dict[str, dict[str, StoredWorkspaceContext]] = {}
        self._active_context_key_by_session: dict[str, str] = {}
        self._bootstrap_instance: SerenaChildInstance | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def _list_tools() -> list[Tool]:
            bootstrap = await self._get_bootstrap_instance()
            tool_map: dict[str, Tool] = {tool.name: tool for tool in await bootstrap.list_tools()}
            for tool in [*COORDINATOR_TOOLS, *LAUNCHER_TOOLS]:
                tool_map[tool.name] = tool
            return list(tool_map.values())

        @self.server.call_tool()
        async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await self._handle_tool_call(tool_name, arguments or {})

    async def run_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(NotificationOptions()),
            )

    async def close(self) -> None:
        await self.registry.close()

    async def _get_bootstrap_instance(self) -> SerenaChildInstance:
        if self._bootstrap_instance is None:
            bootstrap_project = self.config.bootstrap_project_path or self.config.runtime_base_dir
            context = ResolvedRequestContext(
                session_id=self.config.stdio_session_id or "bootstrap",
                workspace_id="__bootstrap__",
                client_id="launcher-bootstrap",
                project_path=str(bootstrap_project),
            )
            self._bootstrap_instance = self.registry.get_or_create(context)
            await self._bootstrap_instance.start()
        return self._bootstrap_instance

    async def _handle_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        request_context = self.server.request_context
        meta = request_context.meta.model_dump(by_alias=True, exclude_none=True) if request_context.meta is not None else {}
        meta_values = dict(meta.get("_meta", meta))
        session_id = self._get_session_id()

        if tool_name in BOOTSTRAP_TOOL_NAMES and not self._has_resolvable_context(arguments, meta_values, session_id):
            bootstrap = await self._get_bootstrap_instance()
            return await bootstrap.call_tool(tool_name, arguments, meta=meta_values)

        resolved = self._resolve_request_context(arguments, meta_values, session_id)

        if tool_name in LAUNCHER_TOOL_NAMES:
            return self._handle_launcher_tool(tool_name, arguments, resolved)
        if tool_name in COORDINATOR_TOOL_NAMES:
            return await self._handle_coordinator_tool(tool_name, arguments, resolved)
        self._guard_shared_memory_writes(tool_name, arguments)
        return await self._handle_forwarded_tool(tool_name, arguments, resolved, meta_values)

    def _get_session_id(self) -> str:
        try:
            return f"session-{id(self.server.request_context.session)}"
        except LookupError:
            return self.config.stdio_session_id or "stdio"

    def _has_resolvable_context(self, arguments: dict[str, Any], meta: dict[str, Any], session_id: str) -> bool:
        try:
            self._resolve_request_context(arguments, meta, session_id)
            return True
        except ValueError:
            return False

    def _get_session_workspace_map(self, session_id: str) -> dict[str, StoredWorkspaceContext]:
        return self._workspace_context_by_session.setdefault(session_id, {})

    def _get_stored_context(self, session_id: str, workspace_id_hint: str | None) -> StoredWorkspaceContext | None:
        workspace_map = self._get_session_workspace_map(session_id)
        if workspace_id_hint and workspace_id_hint in workspace_map:
            return workspace_map[workspace_id_hint]
        active_workspace_id = self._active_context_key_by_session.get(session_id)
        if active_workspace_id and active_workspace_id in workspace_map:
            return workspace_map[active_workspace_id]
        return None

    def _remember_context(self, context: ResolvedRequestContext) -> None:
        workspace_map = self._get_session_workspace_map(context.session_id)
        workspace_map[context.workspace_id] = StoredWorkspaceContext(
            workspace_id=context.workspace_id,
            client_id=context.client_id,
            project_path=context.project_path,
        )
        self._active_context_key_by_session[context.session_id] = context.workspace_id

    def _resolve_request_context(self, arguments: dict[str, Any], meta: dict[str, Any], session_id: str) -> ResolvedRequestContext:
        workspace_hint = _string_or_none(meta.get("workspaceId"))
        stored = self._get_stored_context(session_id, workspace_hint)
        workspace_id = workspace_hint or (stored.workspace_id if stored else None) or self.config.default_workspace_id
        if not workspace_id and self.config.allow_session_fallback:
            workspace_id = f"session-{sanitize_segment(session_id)}"
        if not workspace_id:
            raise ValueError("当前请求缺少 `_meta.workspaceId`，且当前会话没有已激活的工作区上下文。")

        client_id = (
            _string_or_none(meta.get("clientId"))
            or (stored.client_id if stored else None)
            or self.config.default_client_id
        )
        if not client_id:
            raise ValueError("当前请求缺少 `_meta.clientId`。")

        incoming_project_path = _string_or_none(arguments.get("project")) or _string_or_none(arguments.get("project_path")) or _string_or_none(meta.get("projectPath"))
        project_path = incoming_project_path or (stored.project_path if stored else None)
        if not project_path:
            raise ValueError("当前请求缺少 `_meta.projectPath`，且当前会话没有已激活的项目上下文。")

        return ResolvedRequestContext(
            session_id=session_id,
            workspace_id=workspace_id,
            client_id=client_id,
            project_path=str(normalize_project_root(project_path)),
        )

    def _handle_launcher_tool(self, tool_name: str, arguments: dict[str, Any], context: ResolvedRequestContext) -> types.CallToolResult:
        project_path = _string_or_none(arguments.get("project_path")) or context.project_path
        if not project_path:
            raise ValueError("缺少 project_path，无法检查或创建 Serena 配置。")
        if tool_name == "inspect_project_serena":
            state = inspect_project_root_serena_config(project_path)
            return _json_result({"ok": True, "projectPath": project_path, **state.to_payload()})
        if tool_name == "setup_project_serena":
            state = setup_project_root_serena_config(project_path)
            return _json_result({"ok": True, "projectPath": project_path, **state.to_payload()})
        if tool_name == "inspect_codex_serena_binding":
            state = inspect_codex_serena_binding(project_path, include_global=_bool_or_default(arguments.get("include_global"), True))
            return _json_result({"ok": True, "projectPath": project_path, **state.to_payload()})
        if tool_name == "setup_codex_serena_binding":
            state = setup_codex_serena_binding(project_path, update_global=_bool_or_default(arguments.get("update_global"), False))
            return _json_result({"ok": True, "projectPath": project_path, **state.to_payload()})
        raise ValueError(f"未知路由工具：{tool_name}")

    async def _handle_coordinator_tool(self, tool_name: str, arguments: dict[str, Any], context: ResolvedRequestContext) -> types.CallToolResult:
        if tool_name == "claim_scope":
            payload = self.coordinator.claim_scope(
                project_path=context.project_path,
                workspace_id=context.workspace_id,
                agent_id=context.client_id,
                module_name=_string_or_none(arguments.get("module_name")) or "",
                files=_list_of_strings(arguments.get("files")),
                symbols=_list_of_strings(arguments.get("symbols")),
                note=_string_or_none(arguments.get("note")) or "",
                ttl_ms=_int_or_none(arguments.get("ttl_ms")),
            )
        elif tool_name == "release_scope":
            claim_id = _int_or_none(arguments.get("claim_id"))
            if claim_id is None:
                raise ValueError("release_scope 缺少 claim_id。")
            payload = self.coordinator.release_scope(
                project_path=context.project_path,
                claim_id=claim_id,
                workspace_id=context.workspace_id,
                agent_id=context.client_id,
            )
        elif tool_name == "publish_change":
            summary = _string_or_none(arguments.get("summary"))
            if not summary:
                raise ValueError("publish_change 缺少 summary。")
            payload = self.coordinator.publish_change(
                project_path=context.project_path,
                workspace_id=context.workspace_id,
                agent_id=context.client_id,
                module_name=_string_or_none(arguments.get("module_name")) or "",
                summary=summary,
                files=_list_of_strings(arguments.get("files")),
                symbols=_list_of_strings(arguments.get("symbols")),
                compatibility_notes=_string_or_none(arguments.get("compatibility_notes")) or "",
                base_commit=_string_or_none(arguments.get("base_commit")) or "",
            )
        elif tool_name == "read_project_feed":
            payload = {
                "ok": True,
                "items": self.coordinator.read_recent_changes(context.project_path, limit=_int_or_none(arguments.get("limit")) or 20),
            }
        elif tool_name == "read_active_claims":
            payload = {"ok": True, "items": self.coordinator.read_active_claims(context.project_path)}
        else:
            raise ValueError(f"未知协调工具：{tool_name}")

        await self._sync_shared_memories(context)
        return _json_result(payload)

    async def _handle_forwarded_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ResolvedRequestContext,
        meta: dict[str, Any],
    ) -> types.CallToolResult:
        instance = self.registry.get_or_create(context)
        await self._sync_project_root_config_into_instance(context, instance.plan.project_serena_folder)
        await self._sync_shared_memories(context)

        if tool_name == "activate_project":
            state = inspect_project_root_serena_config(context.project_path)
            self._ensure_project_ready_for_activation(state)
            forwarded_arguments = {**arguments, "project": context.project_path}
            result = await instance.call_tool(tool_name, forwarded_arguments, meta=meta)
            if not result.isError:
                instance.active_project_path = context.project_path
                self._remember_context(context)
            return result

        if tool_name not in BOOTSTRAP_TOOL_NAMES:
            state = inspect_project_root_serena_config(context.project_path)
            self._ensure_project_ready_for_activation(state)
            await instance.ensure_activated(context.project_path, meta=meta)
            self._remember_context(context)
        return await instance.call_tool(tool_name, arguments, meta=meta)

    async def _sync_shared_memories(self, context: ResolvedRequestContext) -> None:
        instance = self.registry.get_or_create(context)
        self.coordinator.render_shared_memories(
            project_path=context.project_path,
            project_serena_folder=instance.plan.project_serena_folder,
        )

    async def _sync_project_root_config_into_instance(self, context: ResolvedRequestContext, target_project_serena_folder: Path) -> None:
        root_project_yml = get_project_root_serena_config_path(context.project_path)
        root_project_local_yml = get_project_root_serena_local_config_path(context.project_path)
        target_project_serena_folder.mkdir(parents=True, exist_ok=True)
        if root_project_yml.exists():
            shutil.copy2(root_project_yml, target_project_serena_folder / root_project_yml.name)
        if root_project_local_yml.exists():
            shutil.copy2(root_project_local_yml, target_project_serena_folder / root_project_local_yml.name)
        else:
            target_local = target_project_serena_folder / root_project_local_yml.name
            if target_local.exists():
                target_local.unlink()

    @staticmethod
    def _ensure_project_ready_for_activation(state: ProjectSerenaConfigState) -> None:
        if state.status != "ok":
            raise ValueError(describe_project_root_serena_config_requirement(state))

    @staticmethod
    def _guard_shared_memory_writes(tool_name: str, arguments: dict[str, Any]) -> None:
        if tool_name not in SHARED_MEMORY_MUTATION_TOOL_NAMES:
            return
        names = []
        if tool_name == "rename_memory":
            names.extend([_string_or_none(arguments.get("old_name")), _string_or_none(arguments.get("new_name"))])
        else:
            names.append(_string_or_none(arguments.get("memory_name")))
        if any(name and name.startswith("shared/") for name in names):
            raise ValueError("禁止直接写入或改名 `shared/*`；共享区只能通过协调工具生成。")


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_default(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _list_of_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _tool_result_to_text(result: types.CallToolResult) -> str:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    return "\n".join(texts)
