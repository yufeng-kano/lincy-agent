"""Tests for shared JSON persistence helpers."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

from lincy.json_store import save_json


def test_save_json_supports_concurrent_writes_to_the_same_path(tmp_path):
    path = tmp_path / "state.json"
    workers = 16
    barrier = threading.Barrier(workers)

    def write(index: int) -> None:
        barrier.wait()
        save_json(path, {"writer": index})

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(write, index) for index in range(workers)]
        for future in futures:
            future.result()

    assert json.loads(path.read_text(encoding="utf-8")) in [
        {"writer": index} for index in range(workers)
    ]
