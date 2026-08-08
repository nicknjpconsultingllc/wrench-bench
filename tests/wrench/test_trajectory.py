"""Unit tests for the WRENCH trajectory writer. Pure Python; no server."""

import json

from fle.disruptions.trajectory import TrajectoryWriter


def test_append_and_finalize(tmp_path):
    path = tmp_path / "episode.jsonl"
    writer = TrajectoryWriter(path)
    writer.append_step(0, "sleep(10)", "ok", 600, {"iron-plate": 4.0})
    writer.append_step(1, "place_entity(...)", "placed", 1200, {})
    writer.finalize({"env_id": "iron_plate_sentinel", "steps_completed": 2})

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {
        "step_index": 0,
        "code": "sleep(10)",
        "response": "ok",
        "game_tick": 600,
        "produced_counts": {"iron-plate": 4.0},
    }
    assert lines[1]["step_index"] == 1

    meta = json.loads((tmp_path / "episode.meta.json").read_text())
    assert meta["env_id"] == "iron_plate_sentinel"
    assert meta["steps_completed"] == 2


def test_truncates_stale_file(tmp_path):
    path = tmp_path / "episode.jsonl"
    path.write_text('{"stale": true}\n')
    TrajectoryWriter(path)
    assert path.read_text() == ""


def test_for_env_noop_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("WRENCH_TRAJECTORY_DIR", raising=False)
    assert TrajectoryWriter.for_env("ep") is None

    monkeypatch.setenv("WRENCH_TRAJECTORY_DIR", str(tmp_path))
    writer = TrajectoryWriter.for_env("ep")
    assert writer is not None
    writer.append_step(0, "x", "y", 1, None)
    assert (tmp_path / "ep.jsonl").exists()
