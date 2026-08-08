import base64
import json
import zlib

from fle.env.tools import Tool


class InjectDisruption(Tool):
    """Admin-side control surface for the WRENCH disruption engine.

    Never exposed to agents (admin tools are bound as ``_inject_disruption``
    and hidden from the agent namespace). The heavy lifting happens in
    server.lua: a dedicated nth-tick handler samples tracked item production
    and fires armed disruptions in game time, so injections land even while
    Python is blocked in a wall-clock sleep.
    """

    def __init__(self, connection, game_state):
        super().__init__(connection, game_state)
        self.name = "inject_disruption"
        self.game_state = game_state

    def __call__(self, command: str, payload: dict | None = None):
        response, _ = self.execute(self.player_index, command, payload or {})
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(f"inject_disruption {command}: {response['error']}")
        return response

    def arm(
        self,
        kind: str,
        seed: int,
        quota_item: str,
        quota_per_min: float,
        *,
        quota_fraction: float = 0.5,
        consecutive_windows: int = 2,
        window_ticks: int = 3600,
        delay_ticks: int = 0,
        params: dict | None = None,
    ) -> int:
        """Arm a disruption; returns its engine-side id."""
        response = self(
            "arm",
            {
                "kind": kind,
                "seed": seed,
                "params": params or {},
                "quota_item": quota_item,
                "quota_per_min": quota_per_min,
                "quota_fraction": quota_fraction,
                "consecutive_windows": consecutive_windows,
                "window_ticks": window_ticks,
                "delay_ticks": delay_ticks,
            },
        )
        return int(response["id"])

    def fire_now(self, disruption_id: int) -> None:
        """Skip the precondition; fire on the next scheduler pass (tests)."""
        self("fire_now", {"id": disruption_id})

    def track(self, item: str) -> None:
        """Start sampling an item's cumulative production."""
        self("track", {"item": item})

    def reset_state(self) -> None:
        """Episode boundary: clear armed specs, events, samples, tracking."""
        self("reset_state")

    def _bulk_read(self, lua_expr: str, key: str) -> list[dict]:
        """Fetch a table as zlib+base64 over a raw rcon.print.

        Bulk data bypasses Tool.execute entirely: its dump/slpp round-trip
        corrupts hyphenated strings, long lists, and raw JSON alike.
        """
        raw = self.connection.rcon_client.send_command(
            f"/silent-command rcon.print(helpers.encode_string("
            f"helpers.table_to_json({lua_expr})))"
        )
        decoded = json.loads(zlib.decompress(base64.b64decode(raw))).get(key, [])
        # Factorio's table_to_json turns empty lists into {}
        return decoded if isinstance(decoded, list) else []

    def drain_events(self) -> list[dict]:
        """Return and clear engine events (armed/fired/failed)."""
        return self._bulk_read(
            "(function() local w = storage.wrench or {events={}} "
            "local evs = w.events w.events = {} "
            "return {events=evs, tick=game.tick} end)()",
            "events",
        )

    def samples(self, since_tick: int = 0) -> list[dict]:
        """Sample ring buffer entries newer than since_tick."""
        return self._bulk_read(
            f"(function() local out = {{}} "
            f"for _, s in pairs((storage.wrench or {{samples={{}}}}).samples) do "
            f"if s.tick > {int(since_tick)} then table.insert(out, s) end end "
            f"return {{samples=out, tick=game.tick}} end)()",
            "samples",
        )
