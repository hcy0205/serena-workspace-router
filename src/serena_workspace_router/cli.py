import sys
from collections.abc import Sequence

import anyio
import click

from serena.config.serena_config import LanguageBackend
from serena_workspace_router.server import RouterConfig, WorkspaceSerenaRouterServer

_MAX_CONTENT_WIDTH = 120


def _build_child_args(
    *,
    context: str,
    modes: Sequence[str],
    language_backend: str | None,
    enable_web_dashboard: bool | None,
    open_web_dashboard: bool | None,
    enable_gui_log_window: bool | None,
    log_level: str | None,
    trace_lsp_communication: bool | None,
    tool_timeout: float | None,
) -> list[str]:
    args = ["-c", "from serena.cli import top_level; top_level()", "start-mcp-server", "--transport", "stdio", "--context", context]
    for mode in modes:
        args.extend(["--mode", mode])
    if language_backend:
        args.extend(["--language-backend", language_backend])
    if enable_web_dashboard is not None:
        args.extend(["--enable-web-dashboard", str(enable_web_dashboard).lower()])
    if open_web_dashboard is not None:
        args.extend(["--open-web-dashboard", str(open_web_dashboard).lower()])
    if enable_gui_log_window is not None:
        args.extend(["--enable-gui-log-window", str(enable_gui_log_window).lower()])
    if log_level:
        args.extend(["--log-level", log_level])
    if trace_lsp_communication is not None:
        args.extend(["--trace-lsp-communication", str(trace_lsp_communication).lower()])
    if tool_timeout is not None:
        args.extend(["--tool-timeout", str(tool_timeout)])
    return args


@click.group(name="serena-workspace-router", context_settings={"max_content_width": _MAX_CONTENT_WIDTH})
def main() -> None:
    pass


@main.command("start-mcp-server", context_settings={"max_content_width": _MAX_CONTENT_WIDTH})
@click.option("--context", type=str, default="codex", show_default=True)
@click.option("--mode", "modes", type=str, multiple=True, default=())
@click.option("--language-backend", type=click.Choice([backend.value for backend in LanguageBackend]), default=None)
@click.option("--transport", type=click.Choice(["stdio"]), default="stdio", show_default=True)
@click.option("--enable-web-dashboard", type=bool, is_flag=False, default=None)
@click.option("--open-web-dashboard", type=bool, is_flag=False, default=None)
@click.option("--enable-gui-log-window", type=bool, is_flag=False, default=None)
@click.option("--log-level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]), default=None)
@click.option("--trace-lsp-communication", type=bool, is_flag=False, default=None)
@click.option("--tool-timeout", type=float, default=None)
def start_mcp_server(
    context: str,
    modes: Sequence[str],
    language_backend: str | None,
    transport: str,
    enable_web_dashboard: bool | None,
    open_web_dashboard: bool | None,
    enable_gui_log_window: bool | None,
    log_level: str | None,
    trace_lsp_communication: bool | None,
    tool_timeout: float | None,
) -> None:
    if transport != "stdio":
        raise click.UsageError("router v1 仅支持 stdio transport。")

    child_args = _build_child_args(
        context=context,
        modes=modes,
        language_backend=language_backend,
        enable_web_dashboard=enable_web_dashboard,
        open_web_dashboard=open_web_dashboard,
        enable_gui_log_window=enable_gui_log_window,
        log_level=log_level,
        trace_lsp_communication=trace_lsp_communication,
        tool_timeout=tool_timeout,
    )
    config = RouterConfig.from_environment(child_args=child_args)
    server = WorkspaceSerenaRouterServer(config)
    try:
        anyio.run(server.run_stdio)
    finally:
        anyio.run(server.close)


def top_level() -> None:
    main(standalone_mode=True)


if __name__ == "__main__":
    sys.exit(top_level())
