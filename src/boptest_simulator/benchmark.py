import argparse
import json
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def run_benchmark(
    base_url: str,
    testcase: str,
    steps: int,
    repetitions: int,
    step_seconds: float,
    concurrency: int = 1,
) -> dict:
    if steps < 1 or repetitions < 1 or step_seconds <= 0 or concurrency < 1:
        raise ValueError(
            "steps, repetitions, and concurrency must be positive; step_seconds must be positive"
        )

    url = "{}/simulations".format(base_url.rstrip("/"))

    def run_once(_: int) -> tuple:
        payload = {
            "testcase": testcase,
            "start_time": 0,
            "warmup_period": 0,
            "step": step_seconds,
            "controls": [{} for _ in range(steps)],
        }
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urlopen(request) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            raise RuntimeError(
                "Simulation request failed ({}): {}".format(exc.code, exc.read().decode("utf-8"))
            ) from exc
        except URLError as exc:
            raise RuntimeError("Cannot reach simulator at {}: {}".format(url, exc.reason)) from exc
        return time.perf_counter() - started, result

    actual_concurrency = min(concurrency, repetitions)
    total_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=actual_concurrency) as executor:
        outcomes = list(executor.map(run_once, range(repetitions)))
    total_elapsed = time.perf_counter() - total_started

    latencies: List[float] = [outcome[0] for outcome in outcomes]
    results = [outcome[1] for outcome in outcomes]
    median = statistics.median(latencies)
    ordered = sorted(latencies)
    p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
    simulated_seconds = steps * step_seconds
    return {
        "testcase": testcase,
        "steps_per_run": steps,
        "repetitions": repetitions,
        "concurrency": actual_concurrency,
        "median_seconds": median,
        "p95_seconds": p95,
        "median_steps_per_second": steps / median if median else 0,
        "median_real_time_factor": simulated_seconds / median if median else 0,
        "total_elapsed_seconds": total_elapsed,
        "batch_requests_per_second": repetitions / total_elapsed if total_elapsed else 0,
        "aggregate_steps_per_second": (
            repetitions * steps / total_elapsed if total_elapsed else 0
        ),
        "aggregate_real_time_factor": (
            repetitions * simulated_seconds / total_elapsed if total_elapsed else 0
        ),
        "last_run": results[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark batch BOPTEST simulations.")
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument("--testcase", default="bestest_air")
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--step-seconds", type=float, default=60)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    if args.steps < 1 or args.repetitions < 1 or args.step_seconds <= 0 or args.concurrency < 1:
        parser.error("--steps, --repetitions, --step-seconds, and --concurrency must be positive")
    print(
        json.dumps(
            run_benchmark(
                args.url,
                args.testcase,
                args.steps,
                args.repetitions,
                args.step_seconds,
                args.concurrency,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
