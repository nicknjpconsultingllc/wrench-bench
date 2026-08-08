from fle.env import Position
from fle.env.tools import Tool


class ReportFault(Tool):
    def __init__(self, connection, game_state):
        super().__init__(connection, game_state)
        self.name = "report_fault"
        self.game_state = game_state

    def __call__(self, position: Position, cause: str) -> bool:
        """Declare a suspected fault in the factory at a position.

        :param position: Where the fault is (or is nearest to)
        :param cause: Short description, e.g. "furnace destroyed", "belt gap"
        :return: True when the report was recorded
        """
        response, _ = self.execute(self.player_index, position.x, position.y, str(cause))
        return isinstance(response, dict)
