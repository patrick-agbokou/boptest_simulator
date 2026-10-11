from contextlib import redirect_stdout
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from boptest_simulator.app import create_app
from boptest_simulator.sessions import SessionStore


class FakeFMU:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


class FakeSimulation:
    def __init__(self):
        self.fmu = FakeFMU()
        self.time = 0
        self.step = 60
        self.output_names = ["zone_temperature"]
        self.input_names = ["heating_setpoint"]
        self.fail_advance = False
        self.options = {"result_handling": "binary"}

    def initialize(self, start_time, warmup_period, end_time=None):
        self.time = start_time
        return 200, "initialized", {"time": self.time, "zone_temperature": 20}

    def advance(self, inputs):
        if self.fail_advance:
            return 400, "invalid control", None
        self.time += self.step
        return 200, "advanced", {
            "time": self.time,
            "zone_temperature": 21,
            "heating_setpoint": inputs.get("heating_setpoint", 0),
        }

    def get_inputs(self):
        return 200, "inputs", {"heating_setpoint": {}}

    def get_measurements(self):
        return 200, "measurements", {"zone_temperature": {}}

    def get_step(self):
        return 200, "step", self.step

    def set_step(self, step):
        self.step = step
        return 200, "step changed", {"step": step}

    def get_results(self, point_names, start_time, final_time):
        return 200, "results", {"time": [start_time, final_time]}

    def get_kpis(self):
        return 200, "kpis", {"energy": 0}

    def get_forecast_points(self):
        return 200, "forecast points", {}

    def get_forecast(self, point_names, horizon, interval):
        return 200, "forecast", {"time": [0]}

    def get_scenario(self):
        return 200, "scenario", {}

    def set_scenario(self, scenario):
        return 200, "scenario changed", scenario


class SimulationServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        testcase_path = root / "bestest_air"
        (testcase_path / "models").mkdir(parents=True)
        (testcase_path / "models" / "wrapped.fmu").touch()
        forecast_path = root / "forecast.json"
        forecast_path.touch()
        self.created = []

        self.simulation_factory = FakeSimulation

        def factory(fmu_path, forecast_params_path):
            simulation = self.simulation_factory()
            self.created.append(simulation)
            return simulation

        self.store = SessionStore(root, forecast_path, factory)
        self.client = TestClient(create_app(self.store))

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def test_startup_prints_discovered_testcases(self):
        output = StringIO()
        with redirect_stdout(output):
            with TestClient(create_app(self.store)):
                pass

        self.assertEqual(
            output.getvalue(),
            "Discovered BOPTEST testcase: bestest_air\n",
        )

    def test_startup_reports_when_no_testcases_are_discovered(self):
        root = Path(self.temp_dir.name) / "empty"
        root.mkdir()
        testcase_dir = root / "testcases"
        testcase_dir.mkdir()
        forecast_path = root / "forecast.json"
        forecast_path.touch()
        empty_store = SessionStore(testcase_dir, forecast_path, lambda *_: FakeSimulation())
        output = StringIO()

        with redirect_stdout(output):
            with TestClient(create_app(empty_store)) as client:
                response = client.get("/testcases")

        self.assertEqual(response.json(), [])
        self.assertEqual(output.getvalue(), "No BOPTEST testcases discovered.\n")

    def test_interactive_session_advances_and_closes(self):
        response = self.client.post("/sessions", json={"testcase": "bestest_air"})
        self.assertEqual(response.status_code, 201)
        session_id = response.json()["session_id"]

        response = self.client.post(
            "/sessions/{}/initialize".format(session_id),
            json={"start_time": 0, "warmup_period": 0},
        )
        self.assertEqual(response.json()["status"], 200)

        response = self.client.post(
            "/sessions/{}/advance".format(session_id),
            json={"inputs": {"heating_setpoint": 22}},
        )
        self.assertEqual(response.json()["payload"]["time"], 60)
        self.assertEqual(response.json()["payload"]["heating_setpoint"], 22)

        response = self.client.delete("/sessions/{}".format(session_id))
        self.assertEqual(response.status_code, 204)
        self.assertTrue(self.created[0].fmu.terminated)

    def test_boptest_api_selects_initializes_advances_and_stops(self):
        response = self.client.get("/testcases")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"testcaseid": "bestest_air"}])

        response = self.client.post("/testcases/bestest_air/select")
        self.assertEqual(response.status_code, 200)
        testid = response.json()["testid"]
        self.assertRegex(
            testid,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )

        response = self.client.get("/name/{}".format(testid))
        self.assertEqual(response.json()["payload"], {"name": "bestest_air"})
        self.assertEqual(self.client.get("/status/{}".format(testid)).json(), "Running")

        response = self.client.put(
            "/initialize/{}".format(testid),
            json={"start_time": 0, "warmup_period": 0},
        )
        self.assertEqual(response.json()["status"], 200)

        response = self.client.post("/advance/{}".format(testid))
        self.assertEqual(response.json()["payload"]["time"], 60)

        response = self.client.post(
            "/advance/{}".format(testid),
            json={"heating_setpoint": 22},
        )
        self.assertEqual(response.json()["payload"]["time"], 120)
        self.assertEqual(response.json()["payload"]["heating_setpoint"], 22)

        self.assertEqual(self.client.get("/inputs/{}".format(testid)).status_code, 200)
        self.assertEqual(
            self.client.get("/measurements/{}".format(testid)).status_code,
            200,
        )
        self.assertEqual(self.client.get("/step/{}".format(testid)).json()["payload"], 60)
        self.assertEqual(
            self.client.put(
                "/step/{}".format(testid),
                json={"step": 30},
            ).json()["payload"],
            {"step": 30},
        )
        self.assertEqual(self.client.get("/kpi/{}".format(testid)).status_code, 200)
        self.assertEqual(
            self.client.get("/forecast_points/{}".format(testid)).status_code,
            200,
        )
        self.assertEqual(self.client.get("/scenario/{}".format(testid)).status_code, 200)
        self.assertEqual(
            self.client.put(
                "/scenario/{}".format(testid),
                json={"electricity_price": "dynamic"},
            ).json()["payload"],
            {"electricity_price": "dynamic"},
        )
        self.assertEqual(
            self.client.put(
                "/forecast/{}".format(testid),
                json={"point_names": ["outside_temperature"], "horizon": 3600, "interval": 900},
            ).json()["status"],
            200,
        )
        self.assertEqual(
            self.client.put(
                "/results/{}".format(testid),
                json={"point_names": ["time"], "start_time": 0, "final_time": 60},
            ).json()["status"],
            200,
        )

        response = self.client.put("/stop/{}".format(testid))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "OK")
        self.assertTrue(self.created[0].fmu.terminated)

    def test_boptest_version_endpoint_reads_framework_version(self):
        version_path = Path(self.temp_dir.name) / "version.txt"
        version_path.write_text("0.8.0\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {"BOPTEST_FRAMEWORK_DIR": self.temp_dir.name},
        ):
            response = self.client.get("/version")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"]["version"], "0.8.0")

    def test_batch_simulation_returns_trajectory_and_performance(self):
        response = self.client.post(
            "/simulations",
            json={
                "testcase": "bestest_air",
                "step": 30,
                "controls": [{}, {"heating_setpoint": 21}],
                "record_points": ["time", "zone_temperature"],
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["simulated_seconds"], 60)
        self.assertEqual(result["trajectory"], [
            {"time": 30, "zone_temperature": 21},
            {"time": 60, "zone_temperature": 21},
        ])
        self.assertEqual(result["final_state"]["heating_setpoint"], 21)
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertGreaterEqual(result["steps_per_second"], 0)
        self.assertGreaterEqual(result["real_time_factor"], 0)
        self.assertEqual(self.created[0].options["result_handling"], "memory")
        self.assertTrue(self.created[0].fmu.terminated)

    def test_batch_without_record_points_omits_trajectory(self):
        response = self.client.post(
            "/simulations",
            json={"testcase": "bestest_air", "controls": [{}, {}]},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["trajectory"], [])
        self.assertEqual(result["final_state"]["time"], 120)

    def test_batch_failure_returns_error_and_releases_fmu(self):
        def failing_simulation():
            simulation = FakeSimulation()
            simulation.fail_advance = True
            return simulation

        self.simulation_factory = failing_simulation
        response = self.client.post(
            "/simulations",
            json={"testcase": "bestest_air", "controls": [{"invalid": 1}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "invalid control")
        self.assertTrue(self.created[0].fmu.terminated)

    def test_unknown_testcase_and_session_return_not_found(self):
        self.assertEqual(
            self.client.post("/sessions", json={"testcase": "missing"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/sessions/missing/inputs").status_code,
            404,
        )

    def test_unconfigured_server_reports_framework_requirement(self):
        client = TestClient(create_app())
        response = client.get("/testcases")
        self.assertEqual(response.status_code, 503)
        self.assertIn("BOPTEST_FRAMEWORK_DIR", response.json()["detail"])
        client.close()


if __name__ == "__main__":
    unittest.main()
