storage.actions.get_entities = function(player_index, radius, entity_names_json, position_x, position_y)
    -- Ensure we have a valid character, recreating if necessary
    local player = storage.utils.ensure_valid_character(player_index)

    local position
    if position_x and position_y then
        position = {x = tonumber(position_x), y = tonumber(position_y)}
    else
        position = player.position
    end

    radius = tonumber(radius) or 5
    local entity_names = helpers.json_to_table(entity_names_json) or {}
    local area = {
        {position.x - radius, position.y - radius},
        {position.x + radius, position.y + radius}
    }

    local filter = {}
    if entity_names and #entity_names > 0 then
        filter = {name = entity_names}
    end

    local entities

    if #entity_names > 0 then
        entities = player.surface.find_entities_filtered{area = area, force = player.force, filter=filter}
    else
        entities = player.surface.find_entities_filtered{area = area, force = player.force}
    end

    local result = {}
    for _, entity in ipairs(entities) do
        -- Wrap the entire entity processing in pcall to catch any LuaEntity invalid errors
        local process_success, process_error = pcall(function()
            -- Double-check validity right before accessing any properties
            if not entity.valid then
                return -- Skip silently
            end

            -- Cache the name check separately to avoid race conditions
            local entity_name = entity.name
            if entity_name == 'character' then
                return -- Skip character entities
            end

            -- Now serialize the entity
            local success, serialized = pcall(function()
                -- Final validity check before serialization
                if not entity.valid then
                    return nil
                end
                return storage.utils.serialize_entity(entity)
            end)

            if success and serialized then
                table.insert(result, serialized)
            end
            -- Silently skip failed entities instead of printing warnings
        end)
        -- Silently continue on any error - don't let one bad entity break the whole call
    end

    -- Issue #379: items dropped on the ground are "item-on-ground" entities
    -- belonging to the NEUTRAL force, so the force-filtered query above can
    -- never see them, even though they block entity placement.  Include them
    -- with a minimal plain-data representation (name/position/item/count)
    -- rather than the full serialize_entity, which assumes machine-like
    -- entities (health, inventories, status).
    local include_ground_items = true
    if entity_names and #entity_names > 0 then
        include_ground_items = false
        for _, requested_name in ipairs(entity_names) do
            if requested_name == "item-on-ground" then
                include_ground_items = true
                break
            end
        end
    end

    if include_ground_items then
        local ground_items = player.surface.find_entities_filtered{area = area, type = "item-entity"}
        for _, item_entity in ipairs(ground_items) do
            pcall(function()
                if item_entity.valid and item_entity.stack and item_entity.stack.valid_for_read then
                    table.insert(result, {
                        name = "\"item-on-ground\"",
                        type = "\"item-entity\"",
                        force = "\"neutral\"",
                        position = {x = item_entity.position.x, y = item_entity.position.y},
                        item = "\""..item_entity.stack.name.."\"",
                        count = item_entity.stack.count
                    })
                end
            end)
        end
    end

    return dump(result)
end