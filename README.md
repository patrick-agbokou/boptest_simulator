# BOPTEST Simulator

A lightweight, Python-only HTTP server for running the compiled BOPTEST test cases. It reuses BOPTEST's `TestCase` implementation and FMUs from a local checkout; it does not use Docker or the upstream Node.js server.

The server follows the upstream [testcase layout](https://github.com/ibpsa/project1-boptest/tree/master/testcases) and the [BOPTEST runtime architecture](https://ibpsa.github.io/project1-boptest/docs-design/architecture.html).

## Requirements

- Python 3.10+
- A local BOPTEST checkout with `testcase.py`, `forecast/`, and compiled test cases under `testcases/`
- PyFMI and the Python dependencies used by that checkout

Set `BOPTEST_FRAMEWORK_DIR` to the absolute path of the BOPTEST checkout. The server discovers test cases that contain `models/wrapped.fmu`. Optional overrides are `BOPTEST_TESTCASES_DIR`, `BOPTEST_FORECAST_UNCERTAINTY_PARAMS`, `BOPTEST_SIMULATOR_HOST`, and `BOPTEST_SIMULATOR_PORT`.

## Install and run

Create a Python environment with PyFMI, then install the upstream framework's Python dependencies and this server:

    conda create -n boptest-simulator python=3.11
    conda activate boptest-simulator
    conda install -c conda-forge pyfmi=2.14 libgfortran4
    export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export BOPTEST_FRAMEWORK_DIR=/path/to/project1-boptest
    python -m pip install -r "$BOPTEST_FRAMEWORK_DIR/service/worker/requirements/production.txt"
    python -m pip install -e ".[test]"
    python -m boptest_simulator

At startup, the server prints each discovered compiled testcase, or reports that no testcases were discovered. It listens on `http://127.0.0.1:8001` by default. Interactive API documentation is available at `/docs`. Set `BOPTEST_SIMULATOR_HOST` to `0.0.0.0` to accept connections from other machines.

If loading an FMU fails with `libgfortran.so.4: cannot open shared object file`, install the legacy GNU Fortran runtime into the same Conda environment that runs the server, add that environment's library directory to the dynamic linker's search path, then restart the server from that shell:

    conda activate boptest-simulator
    conda install -c conda-forge libgfortran4
    export LD_LIBRARY_PATH="$CONDA_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    python -m boptest_simulator

## BOPTEST-compatible interactive API

The interactive API follows BOPTEST's testcase selection and `testid` routes. `GET /testcases` returns testcase descriptors; select a testcase to create a simulation and receive its `testid`:

```python
import requests

base_url = "http://127.0.0.1:8001"
testcases = requests.get(f"{base_url}/testcases").json()
testid = requests.post(
    f"{base_url}/testcases/bestest_air/select",
).json()["testid"]

requests.put(
    f"{base_url}/initialize/{testid}",
    json={"start_time": 0, "warmup_period": 0},
).raise_for_status()
state = requests.post(
    f"{base_url}/advance/{testid}",
    json={},
).json()

requests.put(f"{base_url}/stop/{testid}").raise_for_status()
```

The compatible interactive routes include `/version`, `/name/{testid}`, `/status/{testid}`, `/initialize/{testid}`, `/advance/{testid}`, `/stop/{testid}`, `/inputs/{testid}`, `/measurements/{testid}`, `/step/{testid}`, `/results/{testid}`, `/kpi/{testid}`, `/forecast_points/{testid}`, `/forecast/{testid}`, and `/scenario/{testid}`. Their HTTP methods and response envelopes follow BOPTEST. The `testid` is sent in the path as in BOPTEST; local simulation routes do not require an `Authorization` header. Authenticated user/testcase management endpoints are not implemented.

The earlier `/sessions/{session_id}/...` routes remain available as simulator-specific aliases. `GET /testcases` uses the BOPTEST response shape, an array of testcase descriptors.

## Batch simulations

`POST /simulations` accepts a testcase, initialization settings, and one control dictionary per step. `record_points` selects which values to retain for each step, limiting response size.

```python
response = requests.post(
    "http://127.0.0.1:8001/simulations",
    json={
        "testcase": "bestest_air",
        "start_time": 0,
        "warmup_period": 0,
        "step": 60,
        "controls": [{}, {}, {}],
        "record_points": ["time"],
    },
)
response.raise_for_status()
print(response.json())
```

The result includes elapsed time, steps per second, real-time factor, initial and final state, and any selected trajectory points. The benchmark reports median and p95 latency, per-request and aggregate throughput, and real-time factor. Use `--concurrency` to issue independent batch requests in parallel; each request creates a separate FMU instance. Instance creation is serialized, and PyFMI results are kept in memory to prevent concurrent requests from overwriting shared result files. Start with a low concurrency value:

    python -m boptest_simulator.benchmark --testcase bestest_hydronic_heat_pump --steps 24 --step-seconds 3600 --repetitions 5

    python -m boptest_simulator.benchmark --testcase bestest_hydronic_heat_pump --steps 24 --step-seconds 3600 --repetitions 8 --concurrency 2

## MCP adapter

The optional stdio MCP adapter exposes the HTTP server's testcase, interactive-session, and batch-simulation operations as tools. Start the simulator first, then install the MCP extra into a Python environment and launch the adapter:

    python3 -m venv .venv
    .venv/bin/python -m pip install -e ".[mcp]"
    .venv/bin/python -m boptest_simulator.mcp_server

VS Code can launch this adapter using the configuration in `.vscode/mcp.json`. Set `BOPTEST_SIMULATOR_URL` if the HTTP server is not at `http://127.0.0.1:8001`.

## Tests

Install the test extra with `python -m pip install -e ".[test,mcp]"`, then run:

    python -m unittest discover -s tests

Run a single test:

    python -m unittest tests.test_app.SimulationServerTests.test_batch_simulation_returns_trajectory_and_performance

Run one MCP adapter test:

    python -m unittest tests.test_mcp_server.MCPAdapterTests.test_list_testcases_calls_simulator_api
