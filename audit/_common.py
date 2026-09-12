"""Shared helpers for the FLE observation-fidelity audit repro scripts.

Each repro script is standalone-runnable against the live headless server:

    source .venv/bin/activate && python audit/repro_XXX.py

Conventions:
  PASS  -> the bug WAS reproduced against the current code.
  FAIL  -> the bug was NOT reproduced (either it never existed, or it has
           been fixed; the evidence lines say which).
"""

TCP_PORT = 27000


def connect(inventory=None, **kwargs):
    from fle.env.instance import FactorioInstance

    inst = FactorioInstance(
        address="localhost",
        tcp_port=TCP_PORT,
        fast=True,
        inventory=inventory or {},
        **kwargs,
    )
    return inst


def lua(inst, code):
    """Run a raw Lua snippet that rcon.prints a value, return the string."""
    return inst.rcon_client.send_command("/silent-command " + code)


class Reporter:
    def __init__(self, bug_id, title):
        self.bug_id = bug_id
        self.title = title
        self.evidence = []
        print(f"=== {bug_id}: {title} ===")

    def note(self, line):
        self.evidence.append(line)
        print(f"  [evidence] {line}")

    def verdict(self, reproduced, summary=""):
        status = "PASS (bug reproduced)" if reproduced else "FAIL (not reproduced)"
        print(f"RESULT {self.bug_id}: {status}")
        if summary:
            print(f"  {summary}")
        return 0
