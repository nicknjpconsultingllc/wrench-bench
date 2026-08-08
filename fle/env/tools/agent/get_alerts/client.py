import base64
import json
import zlib

from fle.env.tools import Tool


class GetAlerts(Tool):
    def __init__(self, connection, game_state):
        super().__init__(connection, game_state)
        self.name = "get_alerts"
        self.game_state = game_state

    def __call__(self, seconds: int = 10) -> list[str]:
        """Get current warnings about factory problems from the last N seconds.

        :param seconds: How far back to look (in game seconds)
        :return: Human-readable warnings, e.g. "stone-furnace at (14, 0): out of fuel"
        """
        # armored raw read: alert tables carry hyphenated entity names,
        # which the standard dump/slpp round-trip corrupts
        raw = self.connection.rcon_client.send_command(
            f"/silent-command rcon.print(helpers.encode_string(helpers.table_to_json("
            f"{{alerts = storage.get_alerts({int(seconds)})}})))"
        )
        alerts = json.loads(zlib.decompress(base64.b64decode(raw))).get("alerts", [])
        if not isinstance(alerts, list):
            alerts = list(alerts.values()) if isinstance(alerts, dict) else []
        readable = []
        for a in alerts:
            if not isinstance(a, dict):
                continue
            name = str(a.get("entity_name", "entity")).strip("\"'")
            pos = a.get("position", {})
            issues = a.get("issues", [])
            if isinstance(issues, list):
                issues = ", ".join(str(i).strip("\"'") for i in issues)
            readable.append(
                f"{name} at ({pos.get('x', '?')}, {pos.get('y', '?')}): {issues}"
            )
        return readable
