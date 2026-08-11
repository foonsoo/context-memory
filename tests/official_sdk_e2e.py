"""Black-box interoperability checks using the official MCP Python SDK."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
try:
    from mcp.shared.exceptions import MCPError as McpError
except ImportError:  # MCP SDK 1.x spelling
    from mcp.shared.exceptions import McpError


async def connect(stack: AsyncExitStack, command: list[str]) -> ClientSession:
    transport = await stack.enter_async_context(
        stdio_client(StdioServerParameters(command=command[0], args=command[1:]))
    )
    session = await stack.enter_async_context(ClientSession(*transport))
    initialized = await session.initialize()
    server_info = getattr(initialized, "server_info", None) or initialized.serverInfo
    if server_info.name != "context-memory":
        raise AssertionError(initialized)
    return session


async def list_all_tools(session: ClientSession) -> list[str]:
    names: list[str] = []
    cursor = None
    while True:
        try:
            page = await session.list_tools(cursor=cursor)
        except TypeError:  # MCP SDK 2.x moved pagination into params
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
        names.extend(tool.name for tool in page.tools)
        cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
        if cursor is None:
            return names


def result_value(result) -> dict:
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = result.isError
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = result.structuredContent
    if is_error or structured is None:
        raise AssertionError(result)
    return structured["result"]


async def main() -> None:
    executable = Path(sys.executable).with_name("context-memory")
    if not executable.is_file():
        raise AssertionError(f"installed console script was not found: {executable}")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "shared-workspace"
        workspace.mkdir()
        command = [str(executable), "--db", str(root / "memory.db"), "serve", "--transport", "stdio"]

        async with AsyncExitStack() as stack:
            codex = await connect(stack, command)
            claude = await connect(stack, command)

            tool_names = await list_all_tools(codex)
            if len(tool_names) != len(set(tool_names)) or {"project_resolve", "record_event", "read_events_since"} - set(tool_names):
                raise AssertionError(tool_names)

            resolved = result_value(await codex.call_tool("project_resolve", {"cwd": str(workspace)}))
            project_id = resolved["project"]["id"]
            scope_id = resolved["scope_id"]
            codex_session = result_value(await codex.call_tool("session_start", {
                "project_id": project_id, "scope_id": scope_id, "client": "codex", "external_id": "sdk-e2e-codex",
            }))
            first = result_value(await codex.call_tool("record_event", {
                "project_id": project_id, "scope_id": scope_id, "session_id": codex_session["id"],
                "kind": "message", "content": "written by the official SDK codex client",
            }))

            observed = result_value(await claude.call_tool("read_events_since", {
                "project_id": project_id, "scope_id": scope_id, "cursor": 0, "kinds": ["message"],
            }))
            if [event["id"] for event in observed["events"]] != [first["id"]]:
                raise AssertionError(observed)

            claude_session = result_value(await claude.call_tool("session_start", {
                "project_id": project_id, "scope_id": scope_id, "client": "claude-code", "external_id": "sdk-e2e-claude",
            }))
            second = result_value(await claude.call_tool("record_event", {
                "project_id": project_id, "scope_id": scope_id, "session_id": claude_session["id"],
                "kind": "message", "content": "written by the official SDK claude client",
            }))
            source = result_value(await codex.call_tool("get_source", {"event_id": second["id"]}))
            if source["content"] != "written by the official SDK claude client":
                raise AssertionError(source)

            try:
                await codex.call_tool("project_resolve", {})
            except McpError as exc:
                if exc.error.code != -32602:
                    raise AssertionError(exc) from exc
            else:
                raise AssertionError("official SDK did not receive the expected invalid-params error")


if __name__ == "__main__":
    asyncio.run(main())
