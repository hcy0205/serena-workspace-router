import sys
from datetime import timedelta
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from test.serena_workspace_router.test_router import _create_project


def _result_text(result) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def test_router_stdio_bootstrap_and_forwarded_tools_work(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project_root = _create_project(tmp_path, "stdio-project")
    runtime_dir = tmp_path / "runtime"
    coordination_dir = tmp_path / "coordination"
    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir(parents=True, exist_ok=True)
    router_script = Path(__file__).resolve().parent / "router_test_server.py"
    child_script = Path(__file__).resolve().parent / "mock_child_server.py"
    router_stderr = tmp_path / "router-stderr.log"

    async def _run() -> None:
        env = {
            "TEST_RUNTIME_DIR": str(runtime_dir),
            "TEST_COORDINATION_DIR": str(coordination_dir),
            "TEST_BOOTSTRAP_PROJECT": str(bootstrap_dir),
            "TEST_MOCK_CHILD_SCRIPT": str(child_script),
            "PYTHONPATH": str(repo_root / "src"),
        }
        request_meta = {
            "workspaceId": "ws-a",
            "clientId": "codex",
            "projectPath": str(project_root),
        }

        with router_stderr.open("w", encoding="utf-8") as errlog:
            async with stdio_client(
                StdioServerParameters(
                    command=sys.executable,
                    args=[str(router_script)],
                    env=env,
                    cwd=repo_root,
                ),
                errlog=errlog,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()

                    bootstrap_result = await session.call_tool(
                        "initial_instructions",
                        {},
                        read_timeout_seconds=timedelta(seconds=10),
                    )
                    assert bootstrap_result.isError is False
                    assert "Serena instructions manual" in _result_text(bootstrap_result)

                    activate_result = await session.call_tool(
                        "activate_project",
                        {"project": str(project_root)},
                        read_timeout_seconds=timedelta(seconds=10),
                        meta=request_meta,
                    )
                    assert activate_result.isError is False
                    assert "Programming languages: typescript, python" in _result_text(activate_result)

                    find_symbol_result = await session.call_tool(
                        "find_symbol",
                        {"name_path_pattern": "App", "relative_path": "web_ui/src/App.tsx"},
                        read_timeout_seconds=timedelta(seconds=10),
                        meta={"workspaceId": "ws-a", "clientId": "codex"},
                    )
                    assert find_symbol_result.isError is False
                    payload = find_symbol_result.structuredContent
                    assert payload is not None
                    assert payload["matches"][0]["relative_path"] == "web_ui/src/App.tsx"

    anyio.run(_run)
    assert router_stderr.read_text(encoding="utf-8").strip() == ""
