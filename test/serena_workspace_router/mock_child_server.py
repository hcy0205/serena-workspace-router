import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
import yaml
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

ACTIVE_PROJECT: str | None = None


def _tool(name: str, description: str, input_schema: dict[str, Any]) -> Tool:
    return Tool(name=name, description=description, inputSchema=input_schema)


TOOLS = {
    tool.name: tool
    for tool in [
        _tool("activate_project", "激活项目", {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]}),
        _tool(
            "write_memory",
            "写入记忆",
            {
                "type": "object",
                "properties": {
                    "memory_name": {"type": "string"},
                    "content": {"type": "string"},
                    "max_chars": {"type": "number"},
                },
                "required": ["memory_name", "content"],
            },
        ),
        _tool("read_memory", "读取记忆", {"type": "object", "properties": {"memory_name": {"type": "string"}}, "required": ["memory_name"]}),
        _tool("list_memories", "列出记忆", {"type": "object", "properties": {"topic": {"type": "string"}}}),
        _tool(
            "edit_memory",
            "编辑记忆",
            {
                "type": "object",
                "properties": {
                    "memory_name": {"type": "string"},
                    "needle": {"type": "string"},
                    "repl": {"type": "string"},
                    "mode": {"type": "string"},
                },
                "required": ["memory_name", "needle", "repl", "mode"],
            },
        ),
        _tool("delete_memory", "删除记忆", {"type": "object", "properties": {"memory_name": {"type": "string"}}, "required": ["memory_name"]}),
        _tool(
            "rename_memory",
            "重命名记忆",
            {
                "type": "object",
                "properties": {"old_name": {"type": "string"}, "new_name": {"type": "string"}},
                "required": ["old_name", "new_name"],
            },
        ),
        _tool(
            "find_symbol",
            "查找符号",
            {
                "type": "object",
                "properties": {
                    "name_path_pattern": {"type": "string"},
                    "relative_path": {"type": "string"},
                },
                "required": ["name_path_pattern"],
            },
        ),
        _tool("initial_instructions", "初始说明", {"type": "object", "properties": {}}),
        _tool("get_current_config", "当前配置", {"type": "object", "properties": {}}),
    ]
}


def _config_path() -> Path:
    serena_home = Path(os.environ["SERENA_HOME"])
    return serena_home / "serena_config.yml"


def _load_isolated_config() -> dict[str, Any]:
    return yaml.safe_load(_config_path().read_text(encoding="utf-8")) or {}


def _project_serena_folder() -> Path:
    config = _load_isolated_config()
    return Path(config["project_serena_folder_location"])


def _memory_path(memory_name: str) -> Path:
    memory_root = _project_serena_folder() / "memories"
    name = memory_name.removesuffix(".md")
    parts = name.split("/")
    return memory_root.joinpath(*parts[:-1], f"{parts[-1]}.md")


def _languages() -> list[str]:
    project_yml = _project_serena_folder() / "project.yml"
    if not project_yml.exists():
        return []
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8")) or {}
    languages = data.get("languages") or ([data["language"]] if data.get("language") else [])
    return [str(language) for language in languages]


def _text_result(text: str, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def _json_result(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        structuredContent=payload,
        isError=False,
    )


def _list_memory_names() -> list[str]:
    memory_root = _project_serena_folder() / "memories"
    if not memory_root.exists():
        return []
    result = []
    for memory_file in memory_root.rglob("*.md"):
        result.append(memory_file.relative_to(memory_root).with_suffix("").as_posix())
    return sorted(result)


def _find_symbol(name_path_pattern: str, relative_path: str | None) -> dict[str, Any]:
    project_root = Path(ACTIVE_PROJECT or "")
    if relative_path:
        candidates = [project_root / relative_path]
    else:
        candidates = list(project_root.rglob("*"))
    pattern = re.compile(re.escape(name_path_pattern))
    matches = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if pattern.search(line):
                matches.append({"relative_path": candidate.relative_to(project_root).as_posix(), "line": line_number, "preview": line.strip()})
                break
    return {"matches": matches}


SERVER: Server[Any, Any] = Server("mock-serena-child", version="0.1.0")


@SERVER.list_tools()
async def _list_tools() -> list[Tool]:
    return list(TOOLS.values())


@SERVER.call_tool()
async def _call_tool(tool_name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    global ACTIVE_PROJECT

    if tool_name == "activate_project":
        ACTIVE_PROJECT = str(Path(arguments["project"]).resolve())
        languages = ", ".join(_languages())
        return _text_result(f"Activated project: {ACTIVE_PROJECT}\nProgramming languages: {languages}")

    if tool_name == "initial_instructions":
        return _text_result("Read the Serena instructions manual.")

    if tool_name == "get_current_config":
        return _json_result({"activeProject": ACTIVE_PROJECT, "projectSerenaFolder": str(_project_serena_folder())})

    if tool_name == "write_memory":
        path = _memory_path(arguments["memory_name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments["content"], encoding="utf-8")
        return _text_result(f"Memory {arguments['memory_name']} written.")

    if tool_name == "read_memory":
        path = _memory_path(arguments["memory_name"])
        if not path.exists():
            return _text_result(f"Memory file {arguments['memory_name']} not found.")
        return _text_result(path.read_text(encoding="utf-8"))

    if tool_name == "list_memories":
        return _json_result({"memories": _list_memory_names()})

    if tool_name == "edit_memory":
        path = _memory_path(arguments["memory_name"])
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(arguments["needle"], arguments["repl"]), encoding="utf-8")
        return _text_result(f"Memory {arguments['memory_name']} edited.")

    if tool_name == "delete_memory":
        path = _memory_path(arguments["memory_name"])
        if path.exists():
            path.unlink()
        return _text_result(f"Memory {arguments['memory_name']} deleted.")

    if tool_name == "rename_memory":
        old_path = _memory_path(arguments["old_name"])
        new_path = _memory_path(arguments["new_name"])
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if old_path.exists():
            old_path.replace(new_path)
        return _text_result(f"Memory {arguments['old_name']} renamed.")

    if tool_name == "find_symbol":
        return _json_result(_find_symbol(arguments["name_path_pattern"], arguments.get("relative_path")))

    return _text_result(f"Unknown tool: {tool_name}", is_error=True)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await SERVER.run(read_stream, write_stream, SERVER.create_initialization_options(NotificationOptions()))


if __name__ == "__main__":
    anyio.run(_run)
