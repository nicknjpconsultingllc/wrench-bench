"""Fixtures for the observation-fidelity audit tests.

Overrides the parent tests/conftest.py `instance` fixture with a direct
connection to the live server on localhost:27000 so these tests do not
depend on docker CLI discovery.  The parent autouse `_reset_between_tests`
fixture still runs and resets this instance between tests.
"""

import pytest

from fle.env import FactorioInstance

AUDIT_INVENTORY = {
    "stone-furnace": 4,
    "iron-ore": 50,
    "coal": 50,
    "iron-plate": 20,
    "burner-mining-drill": 2,
    "transport-belt": 20,
}


@pytest.fixture(scope="session")
def instance():
    inst = FactorioInstance(
        address="localhost",
        tcp_port=27000,
        fast=True,
        cache_scripts=True,
        inventory=dict(AUDIT_INVENTORY),
    )
    inst.set_speed(5.0)
    inst.default_initial_inventory = dict(inst.initial_inventory)
    try:
        yield inst
    finally:
        inst.cleanup()
