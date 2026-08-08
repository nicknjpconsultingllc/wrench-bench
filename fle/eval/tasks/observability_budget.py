"""Observability budget: meters inspection-style tool calls per episode.

Context (see docs/failure_taxonomy.md F4/F5): in every WRENCH pilot run so
far, agents detected destroyed entities by accident -- a routine
``get_entity(...)`` call on a now-missing entity returns ``None``, and the
agent's own f-string crashes. Nothing currently costs a world query, so
brute-force re-polling is free and there is no incentive to build real
monitoring (scheduled checks, alert-driven triage, etc). This module makes a
subset of inspection tools countable so a task can surface "N calls
remaining" to the agent and penalize/measure over-polling at scoring time.

Design decision: SOFT cap, not hard cap.
    A hard cap (raise/return an error once the budget is exhausted) would be
    the more direct disincentive, but two things point at soft:
    1. The tool-call hook mechanism this is built on
       (``LuaScriptManager.register_pre_tool_hook``) swallows exceptions
       raised by hook callbacks -- both the wrapper in
       ``LuaScriptManager.setup_tools`` and ``execute_pre_tool_hooks`` itself
       catch-and-print (fle/env/lua_manager.py). A hook that raises to block
       the call would silently fail to block it; a real hard cap would need
       to intercept at the namespace/controller level instead of the hook
       API, which is a bigger surface change for a first iteration.
    2. Project philosophy (README "Anti-gaming", docs/failure_taxonomy.md):
       metrics should degrade gracefully and never crash an agent's episode
       over a benchmark-mechanism technicality. A hard stop on the Nth
       ``get_entity`` call turns "did you monitor efficiently" into "did you
       guess the exact budget number," which is a worse signal. A soft cap
       -- calls beyond budget still execute, but are counted and the
       overage is reported at scoring time -- keeps the agent's program
       runnable while still measuring the thing we care about (was
       inspection disciplined, or brute-force).

Which tools are metered:
    - ``get_entity``, ``get_entities``: the direct world-polling primitives;
      exactly the calls implicated in F5's "NoneType is the real detector"
      pattern.
    - ``inspect_inventory``: same category (poll world state for a status
      check) -- an agent could otherwise dodge the budget by fuel-checking
      via inventory sweeps instead of entity queries.
    - ``get_alerts`` is deliberately UNMETERED. It is the one channel that
      lets an agent monitor cheaply (one call covers "anything wrong
      anywhere in the last N seconds") instead of re-querying every entity.
      Leaving it free is the point of the experiment: if a budget on
      get_entity/get_entities/inspect_inventory pushes agents toward
      get_alerts instead of polling, that is the intervention working.
    - ``get_production_stats`` is NOT metered because it is not
      agent-callable in the first place -- it lives under
      ``fle/env/tools/admin/`` and ``LuaScriptManager.setup_tools`` hides
      admin tools behind an underscore-prefixed name
      (``_get_production_stats``) that only internal FLE code (task
      verification, achievement tracking) calls. Metering it would charge
      the agent for the benchmark's own bookkeeping.

Known artifact -- one free call at episode start:
    ``TaskABC.setup()`` (the real entry point used by every driver --
    Inspect, the gym env, the pilot script) calls ``setup_instance()`` and
    then immediately calls ``GameState.from_instance(instance)``, which
    calls ``namespace.inspect_inventory()`` once per agent as framework
    bookkeeping. That call goes through the same hooked namespace attribute
    an agent's own ``inspect_inventory()`` would use, so it is
    indistinguishable from the outside and gets counted -- budgets therefore
    start at ``used == num_agents`` (typically 1), not 0. This is fixed,
    tiny, and identical across every episode/agent, so it does not change
    relative comparisons; it is not worth special-casing around (that would
    mean distinguishing "framework call" from "agent call" through the same
    call path, which the hook API has no way to do).
"""

from typing import Dict, Iterable, Tuple

from fle.env.lua_manager import LuaScriptManager

# Agent-callable inspection tools that count against the budget. See module
# docstring for the per-tool reasoning (why these three, why not
# get_alerts/get_production_stats).
DEFAULT_METERED_TOOLS: Tuple[str, ...] = (
    "get_entity",
    "get_entities",
    "inspect_inventory",
)


class ObservabilityBudget:
    """Counts metered tool calls made by the agent against a soft budget.

    One instance per episode -- construct fresh in ``setup_instance`` so the
    counter resets. ``install()`` wires it into the live ``FactorioInstance``
    via pre-tool hooks; ``status_line()`` is meant to be appended to the
    per-step observation; ``summary()`` is meant to land in
    ``TaskResponse.meta`` at ``verify()`` time.
    """

    def __init__(
        self,
        budget: int,
        metered_tools: Iterable[str] = DEFAULT_METERED_TOOLS,
    ):
        self.budget = budget
        self.metered_tools = tuple(metered_tools)
        self.used = 0
        self.calls_by_tool: Dict[str, int] = {}

    def install(self, instance) -> None:
        """Register a counting pre-tool hook for every metered tool name.

        Defensive against being called twice on the same live
        ``FactorioInstance`` (e.g. a resumed episode re-running
        ``setup_instance``): drops any previously-registered hooks for the
        metered tool names first, so counts from a stale
        ``ObservabilityBudget`` cannot keep incrementing alongside this one.
        """
        existing = getattr(instance, "pre_tool_hooks", None) or {}
        for name in self.metered_tools:
            existing.pop(name, None)
        instance.pre_tool_hooks = existing
        for tool_name in self.metered_tools:
            LuaScriptManager.register_pre_tool_hook(
                instance, tool_name, self._make_hook(tool_name)
            )

    def _make_hook(self, tool_name: str):
        def _count(tool_instance, *args, **kwargs):
            self.used += 1
            self.calls_by_tool[tool_name] = self.calls_by_tool.get(tool_name, 0) + 1

        return _count

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    @property
    def over_budget(self) -> int:
        return max(0, self.used - self.budget)

    def status_line(self) -> str:
        """One line of budget status, meant to be appended to the agent's
        per-step observation (never mentions disruptions -- budget is an
        orthogonal, always-on mechanism)."""
        if self.over_budget:
            return (
                f"Inspection calls remaining: 0/{self.budget} "
                f"(over budget by {self.over_budget} -- calls still work but "
                f"are being counted against you)"
            )
        return f"Inspection calls remaining: {self.remaining}/{self.budget}"

    def summary(self) -> dict:
        """Budget-usage payload for ``TaskResponse.meta`` / trajectory meta."""
        return {
            "inspection_calls_used": self.used,
            "inspection_calls_budget": self.budget,
            "inspection_calls_over_budget": self.over_budget,
            "inspection_calls_within_budget": self.used <= self.budget,
            "inspection_calls_by_tool": dict(self.calls_by_tool),
            "metered_tools": list(self.metered_tools),
        }
