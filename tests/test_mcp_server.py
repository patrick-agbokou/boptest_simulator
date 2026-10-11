import unittest
from unittest.mock import AsyncMock, patch

from mcp import Client

from boptest_simulator import mcp_server


class MCPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_testcases_calls_simulator_api(self):
        with patch(
            "boptest_simulator.mcp_server.request_json",
            new=AsyncMock(return_value=[{"testcaseid": "bestest_air"}]),
        ) as request:
            async with Client(mcp_server.mcp) as client:
                result = await client.call_tool("list_testcases", {})

        self.assertEqual(
            result.structured_content,
            {"result": {"testcases": ["bestest_air"]}},
        )
        request.assert_awaited_once_with("GET", "/testcases")

    async def test_advance_session_escapes_session_identifier(self):
        with patch(
            "boptest_simulator.mcp_server.request_json",
            new=AsyncMock(
                return_value={
                    "status": 200,
                    "message": "advanced",
                    "payload": {"time": 60},
                }
            ),
        ) as request:
            async with Client(mcp_server.mcp) as client:
                result = await client.call_tool(
                    "advance_session",
                    {"session_id": "test/id", "inputs": {"setpoint": 21}},
                )

        self.assertEqual(result.structured_content["result"]["payload"]["time"], 60)
        request.assert_awaited_once_with(
            "POST",
            "/sessions/test%2Fid/advance",
            {"inputs": {"setpoint": 21}},
        )


if __name__ == "__main__":
    unittest.main()
