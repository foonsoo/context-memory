#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from context_memory.hosted_content import (
    HostedContentStore,
    RequestScopedHostedContentStore,
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))
    return ordered[index]


def measure(writers: int, readers: int, operations: int) -> dict[str, object]:
    if writers < 1 or readers < 1 or operations < 1:
        raise ValueError("writers, readers, and operations must be positive")
    with tempfile.TemporaryDirectory() as tempdir:
        database = Path(tempdir) / "load.db"
        initial = HostedContentStore(database)
        initial.provision_tenant("tenant-a")
        initial.provision_project("tenant-a", "project-a")
        initial.close()
        repository = RequestScopedHostedContentStore(
            database, wal_autocheckpoint_pages=16
        )
        barrier = threading.Barrier(writers + readers)
        write_latencies: list[float] = []
        read_latencies: list[float] = []
        lock = threading.Lock()

        def write(worker: int) -> None:
            barrier.wait()
            for index in range(operations):
                started = time.perf_counter()
                repository.record_event(
                    "tenant-a",
                    "project-a",
                    "fact",
                    f"worker-{worker}-event-{index}",
                )
                with lock:
                    write_latencies.append(time.perf_counter() - started)

        def read() -> None:
            barrier.wait()
            for _ in range(operations):
                started = time.perf_counter()
                repository.search("tenant-a", "project-a", "worker")
                with lock:
                    read_latencies.append(time.perf_counter() - started)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=writers + readers) as executor:
            futures = [
                executor.submit(write, worker) for worker in range(writers)
            ]
            futures += [executor.submit(read) for _ in range(readers)]
            for future in futures:
                future.result()
        elapsed = time.perf_counter() - started
        event_count = len(
            repository.export_project("tenant-a", "project-a")["events"]
        )
    total_operations = writers * operations + readers * operations
    return {
        "configuration": {
            "writers": writers,
            "readers": readers,
            "operations_per_worker": operations,
        },
        "results": {
            "elapsed_seconds": elapsed,
            "operations_per_second": total_operations / elapsed,
            "events_committed": event_count,
            "write_latency_ms": {
                "p50": statistics.median(write_latencies) * 1000,
                "p95": percentile(write_latencies, 0.95) * 1000,
                "max": max(write_latencies) * 1000,
            },
            "read_latency_ms": {
                "p50": statistics.median(read_latencies) * 1000,
                "p95": percentile(read_latencies, 0.95) * 1000,
                "max": max(read_latencies) * 1000,
            },
        },
        "interpretation": "development-host measurement; not an SLO",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--operations", type=int, default=100)
    arguments = parser.parse_args()
    print(
        json.dumps(
            measure(
                arguments.writers, arguments.readers, arguments.operations
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
