"""WRENCH test fixtures.

Overrides the parent conftest's session-scoped `instance` fixture with one
that connects straight to the benchmark server on localhost:27000, and
overrides the autouse reset fixture so pure-unit tests (test_scoring.py)
never touch -- or wait for -- a Factorio server.

Live tests are marked `wrench_live`; they are skipped automatically when
nothing is listening on localhost:27000, and can be deselected with
`pytest -m "not wrench_live"`.
"""

import os
import socket

import pytest

# Overridable so multiple concurrent worktrees/agents can each point their
# `wrench_live` tests at their own Factorio container without editing this
# file (mirrors tests/conftest.py's FACTORIO_RCON_PORT override).
WRENCH_RCON_PORT = int(os.environ.get("WRENCH_RCON_PORT", 27000))

# Enough materials to build the two-furnace fixture factory, plus spares for
# the oracle agent's repairs.
WRENCH_TEST_INVENTORY = {
    "stone-furnace": 6,
    "coal": 1000,
    "iron-ore": 1000,
    "transport-belt": 50,
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "wrench_live: requires the live Factorio server on localhost:27000",
    )


def _server_listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def instance():
    """Direct connection to the WRENCH benchmark server (RCON :27000)."""
    if not _server_listening(WRENCH_RCON_PORT):
        pytest.skip(f"no live Factorio server on localhost:{WRENCH_RCON_PORT}")

    from fle.env import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=WRENCH_RCON_PORT,
        all_technologies_researched=True,
        cache_scripts=True,
        fast=True,
        inventory=dict(WRENCH_TEST_INVENTORY),
    )
    inst.set_speed(10.0)
    inst.default_initial_inventory = dict(WRENCH_TEST_INVENTORY)
    try:
        yield inst
    finally:
        inst.cleanup()


@pytest.fixture(autouse=True)
def _reset_between_tests(request):
    """Override the parent autouse fixture: only live tests get an instance
    (and a reset); unit tests run without a server entirely."""
    if request.node.get_closest_marker("wrench_live"):
        inst = request.getfixturevalue("instance")
        inst.initial_inventory = dict(inst.default_initial_inventory)
        inst.reset(reset_position=True)
    yield
