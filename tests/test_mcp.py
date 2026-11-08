import asyncio
from types import SimpleNamespace

import httpx
import pytest
import uvicorn

from memory_mcp.application.extraction import Extractor
from memory_mcp.application.service import MemoryService
from memory_mcp.application.worker import Worker
from memory_mcp.domain.schemas import ProfileView
from memory_mcp.interfaces.mcp import MCPAdapter, create_app
from tests.fakes import FakeModels


async def test_mcp_declares_five_flat_tool_inputs_and_schemas():
    adapter = MCPAdapter(SimpleNamespace())
    assert {tool.name for tool in adapter.tools} == {
        "add",
        "search",
        "get_memory",
        "get_user_profile",
        "edit_user_profile",
    }
    for tool in adapter.tools:
        assert "tag" in tool.input_schema["properties"]
        assert tool.output_schema["type"] == "object"
    result = await adapter.invoke("add", {"content": "missing tag"})
    assert result.is_error
    assert result.structured_content["error"]["code"] == "INVALID_ARGUMENTS"


async def test_empty_profile_keeps_explicit_null_timestamps():
    class Repository:
        async def get_profile(self, tag):
            return ProfileView(tag=tag)

    result = await MCPAdapter(SimpleNamespace(repository=Repository())).invoke(
        "get_user_profile", {"tag": "a"}
    )
    assert result.structured_content == {
        "tag": "a",
        "content": "",
        "version": 0,
        "created_at": None,
        "updated_at": None,
    }


@pytest.mark.integration
async def test_two_http_mcp_clients_share_memory_after_first_disconnects(
    repository, settings, unused_tcp_port
):
    models = FakeModels()
    service = MemoryService(repository, models, models, settings)
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service, settings),
            host="127.0.0.1",
            port=unused_tcp_port,
            log_level="error",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        url = f"http://127.0.0.1:{unused_tcp_port}/mcp"

        async def call(client, method, params, identifier):
            response = await client.post(
                url, json={"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            )
            assert response.status_code == 200, response.text
            data = response.json()
            assert "error" not in data, data
            return data["result"]

        async with httpx.AsyncClient(headers=headers) as client_a:
            await call(
                client_a,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-a", "version": "1"},
                },
                1,
            )
            tools = await call(client_a, "tools/list", {}, 2)
            assert len(tools["tools"]) == 5
            added = await call(
                client_a,
                "tools/call",
                {
                    "name": "add",
                    "arguments": {
                        "tag": "shared",
                        "content": "Atlas 使用 PostgreSQL。",
                        "metadata": {"client": "a"},
                    },
                },
                3,
            )
            raw_id = added["structuredContent"]["raw_id"]
        # Processing continues independently of the first client's connection.
        await Worker(repository, Extractor(repository, models, models), settings).run_once()
        async with httpx.AsyncClient(headers=headers) as client_b:
            await call(
                client_b,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-b", "version": "1"},
                },
                1,
            )
            found = await call(
                client_b,
                "tools/call",
                {"name": "search", "arguments": {"tag": "shared", "query": "Atlas 数据库"}},
                2,
            )
            assert found["structuredContent"]["results"][0]["raw_ids"] == [raw_id]
            wrong_tag = await call(
                client_b,
                "tools/call",
                {"name": "get_memory", "arguments": {"tag": "different", "id": raw_id}},
                3,
            )
            assert wrong_tag["isError"] is True
            assert wrong_tag["structuredContent"]["error"]["code"] == "MEMORY_NOT_FOUND"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
