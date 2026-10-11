import math
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from boptest_simulator.sessions import (
    SessionNotFound,
    SessionStore,
    SimulationSession,
)

_RUNTIME_DIRECTORY: Optional[tempfile.TemporaryDirectory] = None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if hasattr(value, "tolist"):
        return json_safe(value.tolist())
    if hasattr(value, "item"):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    return value


def result_response(result: tuple) -> JSONResponse:
    status, message, payload = result
    return JSONResponse(
        status_code=status,
        content=json_safe({"status": status, "message": message, "payload": payload}),
    )


def require_success(result: tuple) -> Any:
    status, message, payload = result
    if status != 200:
        raise HTTPException(status_code=status, detail=message)
    return payload


def require_state(result: tuple) -> Dict[str, Any]:
    payload = require_success(result)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Simulation returned no state payload.")
    return payload


class CreateSessionRequest(BaseModel):
    testcase: str


class InitializeRequest(BaseModel):
    start_time: float = 0
    warmup_period: float = 0
    end_time: Optional[float] = None


class AdvanceRequest(BaseModel):
    inputs: Dict[str, Any] = Field(default_factory=dict)


class ResultsRequest(BaseModel):
    point_names: List[str]
    start_time: float
    final_time: float


class ForecastRequest(BaseModel):
    point_names: List[str]
    horizon: float
    interval: float


class ScenarioRequest(BaseModel):
    scenario: Dict[str, Any]


class BatchRequest(BaseModel):
    testcase: str
    controls: List[Dict[str, Any]] = Field(max_length=100000)
    start_time: float = 0
    warmup_period: float = 0
    end_time: Optional[float] = None
    step: Optional[float] = None
    record_points: List[str] = Field(default_factory=list)


def load_store_from_environment() -> SessionStore:
    framework_value = os.environ.get("BOPTEST_FRAMEWORK_DIR")
    if not framework_value:
        raise RuntimeError("Set BOPTEST_FRAMEWORK_DIR to a local BOPTEST source checkout.")

    framework_dir = Path(framework_value).expanduser().resolve()
    required_paths = (
        framework_dir / "testcase.py",
        framework_dir / "version.txt",
        framework_dir / "data",
        framework_dir / "kpis",
        framework_dir / "testcases",
        framework_dir / "forecast" / "forecast_uncertainty_params.json",
    )
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise RuntimeError(
            "BOPTEST_FRAMEWORK_DIR is missing required files: {}".format(", ".join(missing))
        )

    framework_path = str(framework_dir)
    if framework_path not in sys.path:
        sys.path.insert(0, framework_path)

    testcases_dir = Path(
        os.environ.get("BOPTEST_TESTCASES_DIR", str(framework_dir / "testcases"))
    ).expanduser()
    forecast_params_path = Path(
        os.environ.get(
            "BOPTEST_FORECAST_UNCERTAINTY_PARAMS",
            str(framework_dir / "forecast" / "forecast_uncertainty_params.json"),
        )
    ).expanduser()
    store = SessionStore(testcases_dir, forecast_params_path)

    global _RUNTIME_DIRECTORY
    _RUNTIME_DIRECTORY = tempfile.TemporaryDirectory(prefix="boptest-simulator-")
    runtime_path = Path(_RUNTIME_DIRECTORY.name)
    shutil.copyfile(framework_dir / "version.txt", runtime_path / "version.txt")
    os.chdir(runtime_path)
    return store


def create_app(store: Optional[SessionStore] = None) -> FastAPI:
    def get_or_load_store(application: FastAPI) -> SessionStore:
        if application.state.store is None:
            with application.state.store_lock:
                if application.state.store is None:
                    application.state.store = load_store_from_environment()
        loaded_store = application.state.store
        if loaded_store is None:
            raise RuntimeError("BOPTEST testcase store failed to initialize.")
        return loaded_store

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        testcase_names = get_or_load_store(application).testcase_names()
        if testcase_names:
            for testcase_name in testcase_names:
                print("Discovered BOPTEST testcase: {}".format(testcase_name), flush=True)
        else:
            print("No BOPTEST testcases discovered.", flush=True)
        yield

    app = FastAPI(
        title="BOPTEST Python Simulator",
        description="Interactive and batch simulation using compiled BOPTEST testcase FMUs.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.store_lock = threading.Lock()

    def get_store(request: Request) -> SessionStore:
        try:
            return get_or_load_store(request.app)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def require_session(
        session_id: str,
        request: Request,
    ) -> Iterator[SimulationSession]:
        yield from access_session(session_id, request)

    def require_testid(
        testid: str,
        request: Request,
    ) -> Iterator[SimulationSession]:
        yield from access_session(testid, request)

    def access_session(
        session_id: str,
        request: Request,
    ) -> Iterator[SimulationSession]:
        session_store = get_store(request)
        try:
            with session_store.access(session_id) as session:
                yield session
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="Unknown simulation session.") from exc

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/testcases")
    def testcases(request: Request) -> List[Dict[str, str]]:
        return [
            {"testcaseid": name}
            for name in get_store(request).testcase_names()
        ]

    @app.post("/testcases/{testcase_name}/select")
    def select_testcase(testcase_name: str, request: Request) -> Dict[str, str]:
        try:
            testid, _ = get_store(request).create_session(testcase_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown testcase.") from exc
        return {"testid": testid}

    @app.get("/version")
    def version(request: Request) -> Dict[str, Any]:
        get_store(request)
        framework_value = os.environ.get("BOPTEST_FRAMEWORK_DIR")
        if not framework_value:
            raise HTTPException(
                status_code=503,
                detail="Set BOPTEST_FRAMEWORK_DIR to a local BOPTEST source checkout.",
            )
        version_path = Path(framework_value).expanduser() / "version.txt"
        try:
            framework_version = version_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Cannot read BOPTEST version file: {}".format(version_path),
            ) from exc
        return {
            "status": 200,
            "message": "Queried the version number successfully.",
            "payload": {"version": framework_version},
        }

    @app.post("/sessions", status_code=201)
    def create_session(
        body: CreateSessionRequest,
        request: Request,
    ) -> Dict[str, str]:
        try:
            session_id, _ = get_store(request).create_session(body.testcase)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown testcase.") from exc
        return {"session_id": session_id, "testcase": body.testcase}

    @app.delete("/sessions/{session_id}", status_code=204)
    def close_session(session_id: str, request: Request) -> None:
        try:
            get_store(request).close_session(session_id)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="Unknown simulation session.") from exc

    @app.put("/stop/{testid}")
    def stop_testcase(testid: str, request: Request) -> PlainTextResponse:
        try:
            get_store(request).close_session(testid)
        except SessionNotFound as exc:
            raise HTTPException(status_code=404, detail="Unknown simulation session.") from exc
        return PlainTextResponse("OK")

    @app.get("/name/{testid}")
    def testcase_name(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response((200, "Queried the name of the test case successfully.", {
            "name": session.testcase_name,
        }))

    @app.get("/status/{testid}")
    def testcase_status(
        session: SimulationSession = Depends(require_testid),
    ) -> str:
        return "Running"

    @app.put("/initialize/{testid}")
    def initialize_testcase(
        body: InitializeRequest,
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        if body.end_time is None:
            result = session.simulation.initialize(body.start_time, body.warmup_period)
        else:
            result = session.simulation.initialize(
                body.start_time,
                body.warmup_period,
                body.end_time,
            )
        return result_response(result)

    @app.post("/advance/{testid}")
    def advance_testcase(
        inputs: Dict[str, Any] = Body(default_factory=dict),
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.advance(inputs))

    @app.get("/inputs/{testid}")
    def testcase_inputs(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_inputs())

    @app.get("/measurements/{testid}")
    def testcase_measurements(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_measurements())

    @app.get("/step/{testid}")
    def testcase_step(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_step())

    @app.put("/step/{testid}")
    def set_testcase_step(
        body: Dict[str, float],
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        try:
            step = body["step"]
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="Missing required field: step") from exc
        return result_response(session.simulation.set_step(step))

    @app.put("/results/{testid}")
    def testcase_results(
        body: ResultsRequest,
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(
            session.simulation.get_results(
                body.point_names,
                body.start_time,
                body.final_time,
            )
        )

    @app.get("/kpi/{testid}")
    def testcase_kpis(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_kpis())

    @app.get("/forecast_points/{testid}")
    def testcase_forecast_points(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_forecast_points())

    @app.put("/forecast/{testid}")
    def testcase_forecast(
        body: ForecastRequest,
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(
            session.simulation.get_forecast(
                body.point_names,
                body.horizon,
                body.interval,
            )
        )

    @app.get("/scenario/{testid}")
    def testcase_scenario(
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.get_scenario())

    @app.put("/scenario/{testid}")
    def set_testcase_scenario(
        scenario: Dict[str, Any],
        session: SimulationSession = Depends(require_testid),
    ) -> JSONResponse:
        return result_response(session.simulation.set_scenario(scenario))

    @app.post("/sessions/{session_id}/initialize")
    def initialize(
        body: InitializeRequest,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        if body.end_time is None:
            result = session.simulation.initialize(body.start_time, body.warmup_period)
        else:
            result = session.simulation.initialize(
                body.start_time,
                body.warmup_period,
                body.end_time,
            )
        return result_response(result)

    @app.post("/sessions/{session_id}/advance")
    def advance(
        body: AdvanceRequest,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        return result_response(session.simulation.advance(body.inputs))

    @app.get("/sessions/{session_id}/inputs")
    def inputs(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_inputs())

    @app.get("/sessions/{session_id}/measurements")
    def measurements(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_measurements())

    @app.get("/sessions/{session_id}/step")
    def get_step(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_step())

    @app.put("/sessions/{session_id}/step")
    def set_step(
        step: float,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        return result_response(session.simulation.set_step(step))

    @app.post("/sessions/{session_id}/results")
    def results(
        body: ResultsRequest,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        return result_response(
            session.simulation.get_results(
                body.point_names,
                body.start_time,
                body.final_time,
            )
        )

    @app.get("/sessions/{session_id}/kpis")
    def kpis(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_kpis())

    @app.get("/sessions/{session_id}/forecast-points")
    def forecast_points(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_forecast_points())

    @app.post("/sessions/{session_id}/forecast")
    def forecast(
        body: ForecastRequest,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        return result_response(
            session.simulation.get_forecast(
                body.point_names,
                body.horizon,
                body.interval,
            )
        )

    @app.get("/sessions/{session_id}/scenario")
    def get_scenario(session: SimulationSession = Depends(require_session)) -> JSONResponse:
        return result_response(session.simulation.get_scenario())

    @app.put("/sessions/{session_id}/scenario")
    def set_scenario(
        body: ScenarioRequest,
        session: SimulationSession = Depends(require_session),
    ) -> JSONResponse:
        return result_response(session.simulation.set_scenario(body.scenario))

    @app.post("/simulations")
    def run_batch(body: BatchRequest, request: Request) -> Dict[str, Any]:
        store = get_store(request)
        try:
            simulation = store.create_simulation(body.testcase)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Unknown testcase.") from exc

        try:
            valid_points = set(simulation.output_names) | set(simulation.input_names) | {"time"}
            unknown_points = sorted(set(body.record_points) - valid_points)
            if unknown_points:
                raise HTTPException(
                    status_code=400,
                    detail="Unknown record_points: {}".format(", ".join(unknown_points)),
                )

            if body.step is not None:
                require_success(simulation.set_step(body.step))
            if body.end_time is None:
                initial_state = require_state(
                    simulation.initialize(body.start_time, body.warmup_period)
                )
            else:
                initial_state = require_state(
                    simulation.initialize(body.start_time, body.warmup_period, body.end_time)
                )

            trajectory = []
            final_state = initial_state
            started = time.perf_counter()
            for controls in body.controls:
                final_state = require_state(simulation.advance(controls))
                if body.record_points:
                    trajectory.append(
                        {
                            name: final_state[name]
                            for name in body.record_points
                            if name in final_state
                        }
                    )
            elapsed = time.perf_counter() - started
            step = require_success(simulation.get_step())
            simulated_seconds = len(body.controls) * step
            return json_safe(
                {
                    "testcase": body.testcase,
                    "steps": len(body.controls),
                    "simulated_seconds": simulated_seconds,
                    "elapsed_seconds": elapsed,
                    "steps_per_second": len(body.controls) / elapsed if elapsed else 0,
                    "real_time_factor": simulated_seconds / elapsed if elapsed else 0,
                    "initial_state": initial_state,
                    "final_state": final_state,
                    "trajectory": trajectory,
                }
            )
        finally:
            store.dispose(simulation)

    return app


app = create_app()
