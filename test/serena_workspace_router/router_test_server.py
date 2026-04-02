import os
import sys
from pathlib import Path

import anyio

from serena_workspace_router.server import RouterConfig, WorkspaceSerenaRouterServer


async def _run() -> None:
    runtime_base_dir = Path(os.environ["TEST_RUNTIME_DIR"]).resolve()
    coordination_base_dir = Path(os.environ["TEST_COORDINATION_DIR"]).resolve()
    bootstrap_project_path = Path(os.environ["TEST_BOOTSTRAP_PROJECT"]).resolve()
    child_script = Path(os.environ["TEST_MOCK_CHILD_SCRIPT"]).resolve()

    config = RouterConfig(
        runtime_base_dir=runtime_base_dir,
        coordination_base_dir=coordination_base_dir,
        child_command=sys.executable,
        child_args=[str(child_script)],
        child_cwd=Path.cwd(),
        default_client_id=os.environ.get("TEST_DEFAULT_CLIENT_ID", "codex"),
        default_workspace_id=os.environ.get("TEST_DEFAULT_WORKSPACE_ID") or None,
        allow_session_fallback=os.environ.get("TEST_ALLOW_SESSION_FALLBACK", "").lower() in {"1", "true", "yes", "on"},
        bootstrap_project_path=bootstrap_project_path,
        stdio_session_id="router-test-stdio",
    )
    server = WorkspaceSerenaRouterServer(config)
    try:
        await server.run_stdio()
    finally:
        await server.close()


if __name__ == "__main__":
    anyio.run(_run)
