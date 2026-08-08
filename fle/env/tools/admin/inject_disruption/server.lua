-- WRENCH disruption engine: armed-spec scheduler + throughput sampler.
-- Runs server-side on a dedicated nth-tick interval so injections fire in
-- game time even while the Python client is blocked in a wall-clock sleep.
-- INTERVAL MUST STAY OFF 60 (and 5/15): place_entity and inspect_inventory
-- register -- and place_entity clears -- on_nth_tick(60) handlers in this
-- same script context; move_to uses 5, harvest_resource 15.

local WRENCH_INTERVAL = 41
local SAMPLE_CAP = 2000

local function wrench_state()
    if not storage.wrench then
        storage.wrench = { armed = {}, events = {}, samples = {}, tracked = {}, next_id = 1 }
    end
    return storage.wrench
end

local function push_event(w, ev)
    ev.tick = game.tick
    table.insert(w.events, ev)
end

-- deterministic pick: one LCG step on the seed, no math.random state
local function seeded_index(seed, n)
    if n == 0 then return nil end
    local x = (seed * 1103515245 + 12345) % 2147483648
    return (x % n) + 1
end

local function sorted_entities(filter)
    local es = game.surfaces[1].find_entities_filtered(filter)
    table.sort(es, function(a, b)
        if a.position.x ~= b.position.x then return a.position.x < b.position.x end
        return a.position.y < b.position.y
    end)
    return es
end

local function manifest_entry(e)
    return { name = e.name, x = e.position.x, y = e.position.y }
end

local KINDS = {}

KINDS.entity_destruction = function(spec)
    local filter = { force = "player" }
    if spec.params.entity_name then
        filter.name = spec.params.entity_name
    else
        filter.type = { "assembling-machine", "furnace", "mining-drill" }
    end
    local es = sorted_entities(filter)
    local idx = seeded_index(spec.seed, #es)
    if not idx then return nil, "no matching entity", true end
    local e = es[idx]
    local m = manifest_entry(e)
    e.die()  -- leaves remnants + spills contents: permanent but observable
    return { m }
end

KINDS.belt_cut = function(spec)
    local count = spec.params.segments or 3
    local belts = sorted_entities({ force = "player", type = "transport-belt" })
    if #belts == 0 then return nil, "no belts", true end
    local manifest = {}
    -- die() the seeded belt, then repeatedly the nearest surviving belt to the
    -- last cut: approximates a contiguous cut without belt-line APIs
    local last = belts[seeded_index(spec.seed, #belts)]
    for _ = 1, count do
        if not (last and last.valid) then break end
        table.insert(manifest, manifest_entry(last))
        local pos = last.position
        last.die()
        local best, bestd = nil, math.huge
        for _, b in pairs(sorted_entities({ force = "player", type = "transport-belt" })) do
            local dx, dy = b.position.x - pos.x, b.position.y - pos.y
            local d = dx * dx + dy * dy
            if d < bestd then best, bestd = b, d end
        end
        last = best
    end
    return manifest
end

KINDS.resource_exhaustion = function(spec)
    local resource = spec.params.resource or "iron-ore"
    local remaining = spec.params.remaining or 1
    local drills = sorted_entities({ force = "player", type = "mining-drill" })
    local idx = seeded_index(spec.seed, #drills)
    if not idx then return nil, "no drills", true end
    local d = drills[idx]
    local radius = d.prototype.mining_drill_radius + 0.01
    local tiles = game.surfaces[1].find_entities_filtered({
        area = { { d.position.x - radius, d.position.y - radius },
                 { d.position.x + radius, d.position.y + radius } },
        name = resource,
    })
    for _, t in pairs(tiles) do t.amount = remaining end
    return { { name = d.name, x = d.position.x, y = d.position.y,
               tiles_changed = #tiles, resource = resource } }
end

-- trailing production rate in items/min, from the sample ring buffer
local function trailing_rate(w, item, window_ticks)
    local n = #w.samples
    if n < 2 then return nil end
    local newest = w.samples[n]
    local base = nil
    for i = n - 1, 1, -1 do
        if newest.tick - w.samples[i].tick >= window_ticks then
            base = w.samples[i]
            break
        end
    end
    if not base then return nil end
    local produced = (newest.counts[item] or 0) - (base.counts[item] or 0)
    local dt = newest.tick - base.tick
    if dt <= 0 then return nil end
    return produced * 3600 / dt
end

script.on_nth_tick(WRENCH_INTERVAL, function(ev)
    local w = wrench_state()
    if next(w.tracked) then
        local stats = game.forces.player.get_item_production_statistics(game.surfaces[1])
        local counts = {}
        for item, _ in pairs(w.tracked) do
            counts[item] = stats.get_input_count(item)  -- input side = produced
        end
        table.insert(w.samples, { tick = ev.tick, counts = counts })
        if #w.samples > SAMPLE_CAP then table.remove(w.samples, 1) end
    end
    for id, spec in pairs(w.armed) do
        if spec.state == "waiting" then
            local rate = trailing_rate(w, spec.quota_item, spec.window_ticks)
            if rate and rate >= spec.quota_per_min * spec.quota_fraction then
                spec.streak = spec.streak + 1
            else
                spec.streak = 0
            end
            if spec.streak >= spec.consecutive_windows then
                spec.state = "armed"
                spec.fire_at = ev.tick + spec.delay_ticks
                push_event(w, { event = "armed", id = id, kind = spec.kind, seed = spec.seed })
            end
        end
        if spec.state == "armed" and ev.tick >= spec.fire_at then
            local manifest, err, design_avoided = KINDS[spec.kind](spec)
            if manifest then
                push_event(w, { event = "fired", id = id, kind = spec.kind,
                                seed = spec.seed, affected = manifest })
            elseif design_avoided then
                -- the agent's factory design made this kind inapplicable
                -- (e.g. belt_cut on a belt-free build): a distinct outcome,
                -- reported separately from fired/failed in scoring
                push_event(w, { event = "not_applicable", id = id,
                                kind = spec.kind, error = err })
            else
                push_event(w, { event = "failed", id = id, kind = spec.kind, error = err })
            end
            w.armed[id] = nil
        end
    end
end)

storage.actions.inject_disruption = function(player, command, payload)
    local w = wrench_state()
    payload = payload or {}
    if command == "arm" then
        if not KINDS[payload.kind] then
            return { error = "unknown kind: " .. tostring(payload.kind) }
        end
        local id = w.next_id
        w.next_id = id + 1
        w.armed[id] = {
            kind = payload.kind,
            seed = payload.seed or 0,
            params = payload.params or {},
            quota_item = payload.quota_item,
            quota_per_min = payload.quota_per_min or 0,
            quota_fraction = payload.quota_fraction or 1.0,
            consecutive_windows = payload.consecutive_windows or 2,
            window_ticks = payload.window_ticks or 3600,
            delay_ticks = payload.delay_ticks or 0,
            state = "waiting",
            streak = 0,
        }
        if payload.quota_item then
            w.tracked[payload.quota_item] = true
        end
        return { id = id, tick = game.tick }
    elseif command == "fire_now" then
        local spec = w.armed[payload.id]
        if not spec then
            return { error = "unknown id: " .. tostring(payload.id) }
        end
        spec.state = "armed"
        spec.fire_at = 0
        return { id = payload.id, tick = game.tick }
    elseif command == "track" then
        w.tracked[payload.item] = true
        return { tick = game.tick }
    elseif command == "reset_state" then
        -- episode boundary: drop armed specs, events, samples, tracking
        storage.wrench = nil
        wrench_state()
        return { tick = game.tick }
    end
    return { error = "unknown command: " .. tostring(command) }
end
