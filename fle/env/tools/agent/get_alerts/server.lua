-- Agent-facing view of the alert system (post-audit: non-destructive,
-- current-within-window, self-invalidating -- see fle/env/mods/alerts.lua).
-- The Python client reads alerts over a raw armored rcon.print because alert
-- tables carry hyphenated entity names, which the dump/slpp round-trip
-- corrupts; this action exists so the tool loads through the standard
-- client.py + server.lua discovery and stays available for Lua-side callers.
storage.actions.get_alerts = function(player, seconds)
    return storage.get_alerts(seconds or 10)
end
