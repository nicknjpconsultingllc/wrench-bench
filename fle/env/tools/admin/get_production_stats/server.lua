storage.actions.production_stats = function(player)
    local production_diff = {}
    local consumption_diff = {}
    local harvested_items = storage.harvested_items
    local crafted_items = storage.crafted_items
    -- Get total production counts for force
    local force = game.forces.player
    local surface = game.surfaces[1]

    -- Factorio 2.0: production_statistics is now a method requiring surface parameter
    local item_stats = force.get_item_production_statistics(surface)
    local fluid_stats = force.get_fluid_production_statistics(surface)

    -- Factorio's LuaFlowStatistics semantics (verified empirically against a
    -- live 2.0.73 server, see audit/repro_prodstats_label_swap.py):
    --   input_counts  = items PRODUCED (the "input" side of the statistics)
    --   output_counts = items CONSUMED
    -- FLE's convention everywhere downstream (profits.py, achievements.py,
    -- observation_formatter.py) is:
    --   "output" = produced, "input" = consumed
    -- Map each engine counter straight to its FLE label.  (Historically this
    -- function contained two inversions -- misnamed locals AND swapped return
    -- keys -- that cancelled out; the external behavior here is unchanged.)
    local item_produced_counts = item_stats.input_counts
    local item_consumed_counts = item_stats.output_counts
    local fluid_produced_counts = fluid_stats.input_counts
    local fluid_consumed_counts = fluid_stats.output_counts

    for name, count in pairs(item_produced_counts) do
        production_diff[name] = count
    end

    for name, count in pairs(item_consumed_counts) do
        consumption_diff[name] = count
    end

    for name, count in pairs(fluid_produced_counts) do
        production_diff[name] = count
    end

    for name, count in pairs(fluid_consumed_counts) do
        consumption_diff[name] = count
    end
    return {
        output = production_diff,   -- produced
        input = consumption_diff,   -- consumed
        harvested = harvested_items,
        crafted = crafted_items
    }
end

storage.actions.reset_production_stats = function(player)
    local force = game.forces.player
    local surface = game.surfaces[1]

    -- Factorio 2.0: production_statistics is now a method requiring surface parameter
    -- Reset item statistics
    force.get_item_production_statistics(surface).clear()

    -- Reset fluid statistics
    force.get_fluid_production_statistics(surface).clear()

    storage.harvested_items = {}
    storage.crafted_items = {}
end

