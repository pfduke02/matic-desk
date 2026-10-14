import json

from mcp.shared.memory import create_connected_server_and_client_session

from matic_desk.mcp_server import TOOL_NAMES, build_server

CONTRACT_TOOLS = {
    "matic_status", "matic_rooms", "matic_bag_report", "matic_history", "matic_map",
    "matic_start_cleaning", "matic_pause", "matic_resume", "matic_stop", "matic_dock",
    "matic_set_setting", "matic_record_bag_swap", "matic_name_room",
}
READ_ONLY = {"matic_status", "matic_rooms", "matic_bag_report", "matic_history", "matic_map"}


async def test_exactly_the_contract_tools(service):
    tools = await build_server(service).list_tools()
    assert {tool.name for tool in tools} == CONTRACT_TOOLS == set(TOOL_NAMES)
    for tool in tools:
        assert tool.description
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.readOnlyHint is (tool.name in READ_ONLY)


async def test_start_cleaning_schema_constrains_mode(service):
    tools = {tool.name: tool for tool in await build_server(service).list_tools()}
    properties = tools["matic_start_cleaning"].inputSchema["properties"]
    assert properties["mode"]["enum"] == ["vacuum", "mop", "both"]
    assert properties["coverage"]["enum"] == ["quick", "standard", "heavy_duty"]
    assert "vacuum" in tools["matic_start_cleaning"].description


async def test_matic_status_over_the_protocol(service):
    async with create_connected_server_and_client_session(build_server(service)) as client:
        result = await client.call_tool("matic_status", {})
    assert result.isError is False
    data = result.structuredContent
    assert data["status"]["activity"] == "docked" and data["settings"]["water_flow"] == 1.0
    assert "docked" in result.content[0].text
    assert json.loads(result.content[1].text) == data


async def test_start_cleaning_and_errors(service, robot):
    async with create_connected_server_and_client_session(build_server(service)) as client:
        ok = await client.call_tool("matic_start_cleaning", {"rooms": ["kitchen"], "mode": "vacuum"})
        missing = await client.call_tool("matic_start_cleaning", {"rooms": ["Garage"]})
        nothing = await client.call_tool("matic_start_cleaning", {})
    assert ok.isError is False and ok.structuredContent["rooms"] == ["Kitchen 1", "Kitchen 2"]
    assert missing.isError is True and "room_not_found" in missing.content[0].text
    assert nothing.isError is True and "invalid_request" in nothing.content[0].text


async def test_map_bag_and_alias_tools(service):
    async with create_connected_server_and_client_session(build_server(service)) as client:
        image = await client.call_tool("matic_map", {"route": False})
        swap = await client.call_tool("matic_record_bag_swap", {"when": "2026-10-10T08:00:00Z"})
        alias = await client.call_tool("matic_name_room", {"alias": "study", "rooms": ["Office"]})
        report = await client.call_tool("matic_bag_report", {})
    assert image.content[0].type == "image" and image.content[0].mimeType == "image/png"
    assert swap.structuredContent["swapped_at"] == "2026-10-10T08:00:00Z"
    assert alias.structuredContent["aliases"] == {"study": ["Office"]}
    assert report.structuredContent["swapped_at"] == "2026-10-10T08:00:00Z"
