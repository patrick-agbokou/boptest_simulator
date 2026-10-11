import json
import threading
import unittest
from unittest.mock import patch

from boptest_simulator.benchmark import run_benchmark


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class BenchmarkTests(unittest.TestCase):
    @patch(
        "boptest_simulator.benchmark.time.perf_counter",
        side_effect=[0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 15.0],
    )
    @patch(
        "boptest_simulator.benchmark.urlopen",
        side_effect=[
            FakeResponse({"run": 1}),
            FakeResponse({"run": 2}),
            FakeResponse({"run": 3}),
        ],
    )
    def test_repeated_batch_benchmark_reports_latency_and_throughput(
        self,
        urlopen,
        perf_counter,
    ):
        result = run_benchmark(
            "http://localhost:8001/",
            "bestest_air",
            steps=10,
            repetitions=3,
            step_seconds=60,
        )

        self.assertEqual(result["median_seconds"], 2.0)
        self.assertEqual(result["p95_seconds"], 4.0)
        self.assertEqual(result["median_steps_per_second"], 5.0)
        self.assertEqual(result["median_real_time_factor"], 300.0)
        self.assertEqual(result["concurrency"], 1)
        self.assertEqual(result["total_elapsed_seconds"], 15.0)
        self.assertEqual(result["batch_requests_per_second"], 0.2)
        self.assertEqual(result["aggregate_steps_per_second"], 2.0)
        self.assertEqual(result["aggregate_real_time_factor"], 120.0)
        self.assertEqual(result["last_run"], {"run": 3})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(perf_counter.call_count, 8)

        request = urlopen.call_args_list[0].args[0]
        self.assertEqual(request.full_url, "http://localhost:8001/simulations")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(len(json.loads(request.data)["controls"]), 10)

    def test_concurrent_benchmark_overlaps_batch_requests(self):
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        active_requests = 0
        max_active_requests = 0

        def concurrent_urlopen(request):
            nonlocal active_requests, max_active_requests
            with lock:
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
            barrier.wait(timeout=5)
            with lock:
                active_requests -= 1
            return FakeResponse({"ok": True})

        with patch("boptest_simulator.benchmark.urlopen", side_effect=concurrent_urlopen):
            result = run_benchmark(
                "http://localhost:8001",
                "bestest_air",
                steps=2,
                repetitions=4,
                step_seconds=60,
                concurrency=2,
            )

        self.assertEqual(max_active_requests, 2)
        self.assertEqual(result["concurrency"], 2)
        self.assertEqual(result["repetitions"], 4)
        self.assertGreater(result["batch_requests_per_second"], 0)
        self.assertGreater(result["aggregate_steps_per_second"], 0)

    @patch(
        "boptest_simulator.benchmark.urlopen",
        return_value=FakeResponse({"status": 200}),
    )
    def test_hydronic_heat_pump_benchmark_targets_requested_testcase(self, urlopen):
        result = run_benchmark(
            "http://localhost:8001",
            "bestest_hydronic_heat_pump",
            steps=24,
            repetitions=1,
            step_seconds=3600,
        )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["testcase"], "bestest_hydronic_heat_pump")
        self.assertEqual(len(payload["controls"]), 24)
        self.assertEqual(payload["step"], 3600)
        self.assertEqual(result["testcase"], "bestest_hydronic_heat_pump")


if __name__ == "__main__":
    unittest.main()
