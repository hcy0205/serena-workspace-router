import sys
from pathlib import Path

import anyio
import yaml
from mcp.types import TextContent

from serena_workspace_router.config import sanitize_segment, sha1_text
from serena_workspace_router.server import ResolvedRequestContext, RouterConfig, WorkspaceSerenaRouterServer


def _write_project_config(project_root: Path, languages: list[str]) -> None:
    serena_dir = project_root / ".serena"
    serena_dir.mkdir(parents=True, exist_ok=True)
    (serena_dir / "project.yml").write_text(
        yaml.safe_dump({"project_name": project_root.name, "languages": languages}, sort_keys=False),
        encoding="utf-8",
    )


def _create_project(tmp_path: Path, name: str) -> Path:
    project_root = tmp_path / name
    (project_root / "web_ui" / "src").mkdir(parents=True)
    (project_root / "python_bridge").mkdir(parents=True)
    (project_root / "web_ui" / "src" / "App.tsx").write_text("export function App() { return null; }\n", encoding="utf-8")
    (project_root / "python_bridge" / "web_main.py").write_text("def _load_env_file_once():\n    return None\n", encoding="utf-8")
    _write_project_config(project_root, ["typescript", "python"])
    return project_root


def _memory_file(runtime_dir: Path, project_root: Path, workspace_id: str, client_id: str, memory_name: str) -> Path:
    workspace_key = f"{sanitize_segment(client_id)}--{sanitize_segment(workspace_id)}"
    project_key = f"{sanitize_segment(project_root.name)}--{sha1_text(str(project_root.resolve()))[:10]}"
    parts = memory_name.split("/")
    return runtime_dir / workspace_key / "projects" / project_key / ".serena" / "memories" / Path(*parts[:-1], f"{parts[-1]}.md")


def _text_from_result(result) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _build_router(router_paths: dict[str, Path]) -> WorkspaceSerenaRouterServer:
    config = RouterConfig(
        runtime_base_dir=router_paths["runtime"],
        coordination_base_dir=router_paths["coordination"],
        child_command=sys.executable,
        child_args=[str(router_paths["mock_child"])],
        child_cwd=router_paths["repo_root"],
        default_client_id="codex",
        bootstrap_project_path=router_paths["bootstrap"],
    )
    return WorkspaceSerenaRouterServer(config)


def _resolved_context(project_root: Path, workspace_id: str, client_id: str, session_id: str = "session-a") -> ResolvedRequestContext:
    return ResolvedRequestContext(
        session_id=session_id,
        workspace_id=workspace_id,
        client_id=client_id,
        project_path=str(project_root.resolve()),
    )


async def _write_private_memory(
    router: WorkspaceSerenaRouterServer,
    *,
    context: ResolvedRequestContext,
    memory_name: str,
    content: str,
) -> None:
    instance = router.registry.get_or_create(context)
    await router._sync_project_root_config_into_instance(context, instance.plan.project_serena_folder)
    await instance.ensure_activated(context.project_path)
    result = await instance.call_tool("write_memory", {"memory_name": memory_name, "content": content})
    assert result.isError is False, _text_from_result(result)


def test_missing_project_path_does_not_fall_back_to_cwd(tmp_path: Path) -> None:
    router_paths = {
        "runtime": tmp_path / "runtime",
        "coordination": tmp_path / "coordination",
        "bootstrap": tmp_path / "bootstrap",
        "repo_root": Path(__file__).resolve().parents[2],
        "mock_child": Path(__file__).resolve().parent / "mock_child_server.py",
    }
    router = _build_router(router_paths)
    try:
        try:
            router._resolve_request_context({"memory_name": "private/demo"}, {"workspaceId": "ws-a", "clientId": "codex"}, "session-a")
        except ValueError as error:
            assert "projectPath" in str(error)
        else:
            raise AssertionError("缺少 projectPath 时不应静默回退。")
    finally:
        anyio.run(router.close)


def test_private_memories_are_isolated_by_workspace_project_and_client(tmp_path: Path) -> None:
    router_paths = {
        "runtime": tmp_path / "runtime",
        "coordination": tmp_path / "coordination",
        "bootstrap": tmp_path / "bootstrap",
        "repo_root": Path(__file__).resolve().parents[2],
        "mock_child": Path(__file__).resolve().parent / "mock_child_server.py",
    }
    project_a = _create_project(tmp_path, "project-a")
    project_b = _create_project(tmp_path, "project-b")
    router = _build_router(router_paths)

    async def _run() -> None:
        try:
            await _write_private_memory(router, context=_resolved_context(project_a, "ws-a", "codex"), memory_name="private/demo", content="workspace-a")
            await _write_private_memory(router, context=_resolved_context(project_a, "ws-b", "codex"), memory_name="private/demo", content="workspace-b")
            await _write_private_memory(router, context=_resolved_context(project_b, "ws-a", "codex"), memory_name="private/demo", content="project-b")
            await _write_private_memory(router, context=_resolved_context(project_a, "ws-a", "agent-b"), memory_name="private/demo", content="client-b")
        finally:
            await router.close()

    anyio.run(_run)

    assert _memory_file(router_paths["runtime"], project_a, "ws-a", "codex", "private/demo").read_text(encoding="utf-8") == "workspace-a"
    assert _memory_file(router_paths["runtime"], project_a, "ws-b", "codex", "private/demo").read_text(encoding="utf-8") == "workspace-b"
    assert _memory_file(router_paths["runtime"], project_b, "ws-a", "codex", "private/demo").read_text(encoding="utf-8") == "project-b"
    assert _memory_file(router_paths["runtime"], project_a, "ws-a", "agent-b", "private/demo").read_text(encoding="utf-8") == "client-b"


def test_activate_project_allows_omitting_project_path_in_same_session(tmp_path: Path) -> None:
    router_paths = {
        "runtime": tmp_path / "runtime",
        "coordination": tmp_path / "coordination",
        "bootstrap": tmp_path / "bootstrap",
        "repo_root": Path(__file__).resolve().parents[2],
        "mock_child": Path(__file__).resolve().parent / "mock_child_server.py",
    }
    project_root = _create_project(tmp_path, "project-activate")
    router = _build_router(router_paths)
    context = _resolved_context(project_root, "ws-a", "codex")

    async def _run() -> None:
        try:
            await _write_private_memory(router, context=context, memory_name="private/after_activate", content="ok")
        finally:
            await router.close()

    anyio.run(_run)
    router._remember_context(context)
    resolved = router._resolve_request_context({"memory_name": "private/after_activate"}, {"workspaceId": "ws-a", "clientId": "codex"}, "session-a")
    assert resolved.project_path == context.project_path
    assert _memory_file(router_paths["runtime"], project_root, "ws-a", "codex", "private/after_activate").read_text(encoding="utf-8") == "ok"


def test_shared_feed_and_claims_are_project_scoped(tmp_path: Path) -> None:
    router_paths = {
        "runtime": tmp_path / "runtime",
        "coordination": tmp_path / "coordination",
        "bootstrap": tmp_path / "bootstrap",
        "repo_root": Path(__file__).resolve().parents[2],
        "mock_child": Path(__file__).resolve().parent / "mock_child_server.py",
    }
    project_a = _create_project(tmp_path, "project-feed-a")
    project_b = _create_project(tmp_path, "project-feed-b")
    router = _build_router(router_paths)
    context_a = _resolved_context(project_a, "ws-a", "codex")
    context_a_other_workspace = _resolved_context(project_a, "ws-b", "agent-b")
    context_b = _resolved_context(project_b, "ws-b", "agent-b")

    async def _prepare_shared_dirs() -> tuple[Path, Path, Path]:
        instance_a = router.registry.get_or_create(context_a)
        instance_a_other = router.registry.get_or_create(context_a_other_workspace)
        instance_b = router.registry.get_or_create(context_b)
        await router._sync_project_root_config_into_instance(context_a, instance_a.plan.project_serena_folder)
        await router._sync_project_root_config_into_instance(context_a_other_workspace, instance_a_other.plan.project_serena_folder)
        await router._sync_project_root_config_into_instance(context_b, instance_b.plan.project_serena_folder)
        return instance_a.plan.project_serena_folder, instance_a_other.plan.project_serena_folder, instance_b.plan.project_serena_folder

    same_project_dir = tmp_path / "__same_project_dir__"
    other_workspace_dir = tmp_path / "__other_workspace_dir__"
    other_project_dir = tmp_path / "__other_project_dir__"

    async def _run() -> None:
        nonlocal same_project_dir, other_workspace_dir, other_project_dir
        try:
            same_project_dir, other_workspace_dir, other_project_dir = await _prepare_shared_dirs()
        finally:
            await router.close()

    anyio.run(_run)

    publish_payload = router.coordinator.publish_change(
        project_path=project_a,
        workspace_id="ws-a",
        agent_id="codex",
        summary="router change",
        files=["src/app.ts"],
        symbols=["App"],
    )
    assert publish_payload["ok"] is True

    claim_a = router.coordinator.claim_scope(
        project_path=project_a,
        workspace_id="ws-a",
        agent_id="codex",
        files=["src/app.ts"],
        symbols=["App"],
    )
    claim_same_project = router.coordinator.claim_scope(
        project_path=project_a,
        workspace_id="ws-b",
        agent_id="agent-b",
        files=["src/app.ts"],
        symbols=["App"],
    )
    claim_other_project = router.coordinator.claim_scope(
        project_path=project_b,
        workspace_id="ws-b",
        agent_id="agent-b",
        files=["src/app.ts"],
        symbols=["App"],
    )

    router.coordinator.render_shared_memories(project_path=project_a, project_serena_folder=same_project_dir)
    router.coordinator.render_shared_memories(project_path=project_a, project_serena_folder=other_workspace_dir)
    router.coordinator.render_shared_memories(project_path=project_b, project_serena_folder=other_project_dir)

    same_project_feed = (same_project_dir / "memories" / "shared" / "project_recent_changes.md").read_text(encoding="utf-8")
    other_workspace_feed = (other_workspace_dir / "memories" / "shared" / "project_recent_changes.md").read_text(encoding="utf-8")
    other_project_feed = (other_project_dir / "memories" / "shared" / "project_recent_changes.md").read_text(encoding="utf-8")

    assert "router change" in same_project_feed
    assert "router change" in other_workspace_feed
    assert "router change" not in other_project_feed
    assert claim_a["status"] == "granted"
    assert claim_same_project["status"] == "hard_conflict"
    assert claim_other_project["status"] == "granted"


def test_router_launcher_tools_cover_codex_binding_inspect_and_setup(tmp_path: Path, monkeypatch) -> None:
    router_paths = {
        "runtime": tmp_path / "runtime",
        "coordination": tmp_path / "coordination",
        "bootstrap": tmp_path / "bootstrap",
        "repo_root": Path(__file__).resolve().parents[2],
        "mock_child": Path(__file__).resolve().parent / "mock_child_server.py",
    }
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    project_root = tmp_path / "router-binding-project"
    project_root.mkdir()
    router = _build_router(router_paths)
    context = _resolved_context(project_root, "ws-a", "codex")

    try:
        inspect_result = router._handle_launcher_tool(
            "inspect_codex_serena_binding",
            {"project_path": str(project_root), "include_global": False},
            context,
        )
        inspect_payload = inspect_result.structuredContent
        assert inspect_payload is not None
        assert inspect_payload["status"] == "missing"

        setup_result = router._handle_launcher_tool(
            "setup_codex_serena_binding",
            {"project_path": str(project_root), "update_global": False},
            context,
        )
        setup_payload = setup_result.structuredContent
        assert setup_payload is not None
        assert setup_payload["status"] == "created"
        assert setup_payload["project_binding_ok"] is True
    finally:
        anyio.run(router.close)
