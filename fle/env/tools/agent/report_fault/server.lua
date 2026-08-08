-- Overt fault declaration: the agent's detection signal for WRENCH scoring.
-- In-game no-op; appends to the same event stream the disruption engine
-- writes, so detection latency/precision are computed off one ledger.
storage.actions.report_fault = function(player, x, y, cause)
    if not storage.wrench then
        storage.wrench = { armed = {}, events = {}, samples = {}, tracked = {}, next_id = 1 }
    end
    table.insert(storage.wrench.events, {
        event = "report_fault",
        x = x,
        y = y,
        cause = cause,
        tick = game.tick,
    })
    return { tick = game.tick }
end
