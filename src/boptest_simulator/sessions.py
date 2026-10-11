import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple


TestCaseFactory = Callable[[Path, Path], Any]


def create_testcase(fmu_path: Path, forecast_params_path: Path) -> Any:
    from testcase import TestCase

    return TestCase(
        fmupath=str(fmu_path),
        forecast_uncertainty_params_path=str(forecast_params_path),
    )


@dataclass
class SimulationSession:
    testcase_name: str
    simulation: Any
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class SessionNotFound(LookupError):
    pass


class SessionStore:
    def __init__(
        self,
        testcases_dir: Path,
        forecast_params_path: Path,
        testcase_factory: TestCaseFactory = create_testcase,
    ) -> None:
        self.testcases_dir = Path(testcases_dir).resolve()
        self.forecast_params_path = Path(forecast_params_path).resolve()
        if not self.testcases_dir.is_dir():
            raise RuntimeError("Testcase directory does not exist: {}".format(self.testcases_dir))
        if not self.forecast_params_path.is_file():
            raise RuntimeError(
                "Forecast parameters file does not exist: {}".format(self.forecast_params_path)
            )
        self._testcases = {
            path.name: path
            for path in sorted(self.testcases_dir.iterdir())
            if path.is_dir() and (path / "models" / "wrapped.fmu").is_file()
        }
        self._testcase_factory = testcase_factory
        self._sessions: Dict[str, SimulationSession] = {}
        self._guard = threading.Lock()
        self._factory_lock = threading.Lock()

    def testcase_names(self) -> List[str]:
        return list(self._testcases)

    def _testcase_path(self, testcase_name: str) -> Path:
        testcase_path = self._testcases.get(testcase_name)
        if testcase_path is None:
            raise KeyError(testcase_name)
        return testcase_path

    def create_simulation(self, testcase_name: str) -> Any:
        testcase_path = self._testcase_path(testcase_name)
        with self._factory_lock:
            simulation = self._testcase_factory(
                testcase_path / "models" / "wrapped.fmu",
                self.forecast_params_path,
            )
            options = getattr(simulation, "options", None)
            if options is not None:
                options["result_handling"] = "memory"
            return simulation

    def create_session(self, testcase_name: str) -> Tuple[str, Any]:
        simulation = self.create_simulation(testcase_name)
        session_id = str(uuid.uuid4())
        with self._guard:
            self._sessions[session_id] = SimulationSession(testcase_name, simulation)
        return session_id, simulation

    @contextmanager
    def access(self, session_id: str) -> Iterator[SimulationSession]:
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.lock.acquire()
        try:
            yield session
        finally:
            session.lock.release()

    def close_session(self, session_id: str) -> None:
        with self._guard:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            session.lock.acquire()
            del self._sessions[session_id]
        try:
            self.dispose(session.simulation)
        finally:
            session.lock.release()

    @staticmethod
    def dispose(simulation: Any) -> None:
        fmu = getattr(simulation, "fmu", None)
        terminate = getattr(fmu, "terminate", None)
        if callable(terminate):
            terminate()
