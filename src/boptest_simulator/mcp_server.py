import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
from mcp.server import MCPServer


mcp = MCPServer(
    "BOPTEST Simulator",
    instructions="Use these tools to interact with the separately running BOPTEST Python simulator.",
)


def _base_url() -> str:
    return os.environ.get("BOPTEST_SIMULATOR_URL", "http://127.0.0.1:8001").rstrip("/")


def _session_path(session_id: str, suffix: str = "") -> str:
    path = "/sessions/{}".format(quote(session_id, safe=""))
    return "{}/{}".format(path, suffix) if suffix else path


async def request_json(
    method: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
) -> Any:
    async with httpx.AsyncClient(base_url=_base_url(), timeout=None) as client:
        response = await client.request(method, path, json=body)
        response.raise_for_status()
        if response.status_code == 204:
            return {"closed": True}
        return response.json()


@mcp.tool()
async def list_testcases() -> Dict[str, List[str]]:
    """List compiled BOPTEST testcases available to the simulator."""
    testcases = await request_json("GET", "/testcases")
    return {"testcases": [testcase["testcaseid"] for testcase in testcases]}


@mcp.tool()
async def create_session(testcase: str) -> Dict[str, str]:
    """Create an interactive simulation session for a testcase."""
    return await request_json("POST", "/sessions", {"testcase": testcase})


@mcp.tool()
async def initialize_session(
    session_id: str,
    start_time: float = 0,
    warmup_period: float = 0,
    end_time: Optional[float] = None,
) -> Dict[str, Any]:
    """Initialize an interactive simulation session."""
    body: Dict[str, Any] = {
        "start_time": start_time,
        "warmup_period": warmup_period,
    }
    if end_time is not None:
        body["end_time"] = end_time
    return await request_json("POST", _session_path(session_id, "initialize"), body)


@mcp.tool()
async def advance_session(
    session_id: str,
    inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Advance a session by one control step using the provided testcase inputs."""
    return await request_json(
        "POST",
        _session_path(session_id, "advance"),
        {"inputs": inputs or {}},
    )


@mcp.tool()
async def get_inputs(session_id: str) -> Dict[str, Any]:
    """Get control input names and metadata for a session."""
    return await request_json("GET", _session_path(session_id, "inputs"))


@mcp.tool()
async def get_measurements(session_id: str) -> Dict[str, Any]:
    """Get measurement names and metadata for a session."""
    return await request_json("GET", _session_path(session_id, "measurements"))


@mcp.tool()
async def get_step(session_id: str) -> Dict[str, Any]:
    """Get the current communication step in seconds."""
    return await request_json("GET", _session_path(session_id, "step"))


@mcp.tool()
async def set_step(session_id: str, step: float) -> Dict[str, Any]:
    """Set the communication step in seconds."""
    return await request_json(
        "PUT",
        "{}?step={}".format(_session_path(session_id, "step"), step),
    )


@mcp.tool()
async def get_results(
    session_id: str,
    point_names: List[str],
    start_time: float,
    final_time: float,
) -> Dict[str, Any]:
    """Read selected input and measurement trajectories over a time range."""
    return await request_json(
        "POST",
        _session_path(session_id, "results"),
        {
            "point_names": point_names,
            "start_time": start_time,
            "final_time": final_time,
        },
    )


@mcp.tool()
async def get_kpis(session_id: str) -> Dict[str, Any]:
    """Get the current KPI results for a session."""
    return await request_json("GET", _session_path(session_id, "kpis"))


@mcp.tool()
async def get_forecast_points(session_id: str) -> Dict[str, Any]:
    """Get forecast point names and metadata for a session."""
    return await request_json("GET", _session_path(session_id, "forecast-points"))


@mcp.tool()
async def get_forecast(
    session_id: str,
    point_names: List[str],
    horizon: float,
    interval: float,
) -> Dict[str, Any]:
    """Get forecast values for selected points and a time horizon."""
    return await request_json(
        "POST",
        _session_path(session_id, "forecast"),
        {
            "point_names": point_names,
            "horizon": horizon,
            "interval": interval,
        },
    )


@mcp.tool()
async def get_scenario(session_id: str) -> Dict[str, Any]:
    """Get the active simulation scenario."""
    return await request_json("GET", _session_path(session_id, "scenario"))


@mcp.tool()
async def set_scenario(session_id: str, scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Update scenario options such as electricity price or uncertainty."""
    return await request_json(
        "PUT",
        _session_path(session_id, "scenario"),
        {"scenario": scenario},
    )


@mcp.tool()
async def close_session(session_id: str) -> Dict[str, bool]:
    """Close a session and release its FMU resources."""
    return await request_json("DELETE", _session_path(session_id))


@mcp.tool()
async def run_batch(
    testcase: str,
    controls: List[Dict[str, Any]],
    start_time: float = 0,
    warmup_period: float = 0,
    step: Optional[float] = None,
    end_time: Optional[float] = None,
    record_points: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run a testcase for a sequence of per-step control dictionaries."""
    body: Dict[str, Any] = {
        "testcase": testcase,
        "controls": controls,
        "start_time": start_time,
        "warmup_period": warmup_period,
    }
    if step is not None:
        body["step"] = step
    if end_time is not None:
        body["end_time"] = end_time
    if record_points is not None:
        body["record_points"] = record_points
    return await request_json("POST", "/simulations", body)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
