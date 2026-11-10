-- AutoInformant Pro — independent openOMSI companion bridge v1
-- Plain Lua plugin. Uses only the documented openOMSI plugin API.
-- Audio, route selection and navigation are separate channels; one cannot overwrite another.

local BRIDGE_PORT = 47821
local COMMAND_FILE = "bridge_command.json"
local MUSIC_FILE = "bridge_music.json"
local ROUTE_FILE = "bridge_route.json"
local NAV_FILE = "bridge_nav.json"
local SETTINGS_FILE = "bridge_settings.json"
local POLL_SECONDS = 0.05
local STATE_INTERVAL = 0.35
local MAX_UDP_MESSAGE_BYTES = 7000 -- openOMSI local-send API refuses messages above 8 KB
local state_sequence = 0

local last_command_id = nil
local last_route_id = nil
local last_nav_id = nil
local last_settings_id = nil
local active_sound_id = nil
local active_outside_sound_id = nil
local active_command_id = nil
local active_source = nil
local active_options = nil
local active_then_close_doors = false
local active_command_base_volume = 0.9
local active_base_volume = 0.9
local active_exterior_volume = 0.25
local active_current_volume = 0.9
local active_outside_current_volume = 0.0
local informant_output_level = 1.0
-- Music uses its own pair of sources so a stop announcement can play over it or replace it
-- without destroying the playlist's track state.
local last_music_command_id = nil
local music_sound_id = nil
local music_outside_sound_id = nil
local music_command_id = nil
local music_track_name = nil
local music_source = nil
local music_options = nil
local music_command_base_volume = 0.55
local music_base_volume = 0.55
local music_exterior_volume = 0.30
local music_current_volume = 0.55
local music_outside_current_volume = 0.0
local music_output_level = 1.0
local music_last_audio_update = 0
local music_queued_command = nil
-- openOMSI native internet radio is the preferred path: continuous stream, proper 3D/Doppler, and bus-window exterior sound.
local native_radio = nil
local native_radio_zero_since = nil
local native_radio_last_apply = 0
local MIN_LIVE_VOLUME = 0.0001 -- Keep inactive crossfade variant playing to prevent camera-switch cut-outs.
local active_last_audio_update = 0
local doppler_history = {}
local last_ready_at = -1000
local last_error_at = -1000
local last_state_at = -1000
local last_duty_identity = nil
local poll_tick = 0
local app_route = { map = "", route = "", stops = {} }
local route_mode = "manual" -- auto | manual | pending
local route_reason = "waiting for the app route"
local route_pending_until = -1
local route_pending_trip = nil
local nav_echo_pending = false
local last_nav_target_index = nil
local settings = {
    exterior_volume = 0.25,
    music_exterior_volume = 0.30,
    exterior_range_factor = 0.55,
    exterior_range = 45.0,
    auto_start_matching_trip = true,
    follow_game_progress = true,
    allow_manual_ibis_sync = true,
    range = 7.0,
    source_mode = "ceiling",
    -- Offsets move the source away from the driver's head/camera position.
    -- Coordinates are relative to the vehicle origin (x east, y north, z up).
    speaker_forward = -1.65,
    speaker_right = 0.35,
    speaker_up = 2.25,
}

local function clamp(v, lo, hi, fallback)
    if type(v) ~= "number" then return fallback end
    if v < lo then return lo end
    if v > hi then return hi end
    return v
end

local function has_bus_var(name)
    local ok, value = pcall(omsi.var, name)
    return ok and value ~= nil
end

local function safe_string(v)
    if v == nil then return "" end
    return tostring(v)
end

local function send_event(event)
    local ok, encoded = pcall(omsi.json.encode, event)
    if not ok or type(encoded) ~= "string" then
        omsi.log("AutoInformant Pro: could not encode bridge event")
        return false
    end
    if #encoded > MAX_UDP_MESSAGE_BYTES then
        if (omsi.time() - last_error_at) > 5 then
            last_error_at = omsi.time()
            omsi.log("AutoInformant Pro: outgoing event exceeded the safe UDP size; split it into chunks")
        end
        return false
    end
    local sent, reason = omsi.send(BRIDGE_PORT, encoded)
    if not sent and (omsi.time() - last_error_at) > 5 then
        last_error_at = omsi.time()
        omsi.log("AutoInformant Pro: UDP status send failed: " .. tostring(reason))
    end
    return sent == true
end

local function send_game_state(event, stop_objects, stop_names)
    state_sequence = state_sequence + 1
    event.state_seq = state_sequence
    event.duty_stop_objects = stop_objects
    event.duty_stops = stop_names

    local ok, encoded = pcall(omsi.json.encode, event)
    if ok and type(encoded) == "string" and #encoded <= MAX_UDP_MESSAGE_BYTES then
        send_event(event)
        return
    end

    -- UDP send payloads are limited to 8 KB. Long duties (large maps, many stops or
    -- long Unicode names) must not silently disappear because one JSON datagram is too big.
    local chunks = {}
    local current = {}
    for _, stop in ipairs(stop_objects) do
        local candidate = {}
        for _, item in ipairs(current) do candidate[#candidate + 1] = item end
        candidate[#candidate + 1] = stop
        local probe = {
            type = "duty_stop_chunk", state_seq = state_sequence,
            chunk_index = 99999, chunk_count = 99999, stops = candidate,
        }
        local probe_ok, probe_encoded = pcall(omsi.json.encode, probe)
        if probe_ok and type(probe_encoded) == "string"
            and #probe_encoded > MAX_UDP_MESSAGE_BYTES and #current > 0 then
            chunks[#chunks + 1] = current
            current = { stop }
        else
            current = candidate
        end
    end
    if #current > 0 then chunks[#chunks + 1] = current end

    local base = {}
    for key, value in pairs(event) do
        if key ~= "duty_stops" and key ~= "duty_stop_objects" then base[key] = value end
    end
    base.stops_chunked = true
    base.duty_stop_count = #stop_objects
    base.duty_stop_chunks = #chunks
    send_event(base)

    for i, chunk in ipairs(chunks) do
        send_event({
            type = "duty_stop_chunk", state_seq = state_sequence,
            chunk_index = i, chunk_count = #chunks, stops = chunk,
        })
    end
end

local function read_json_file(path)
    local ok, content = pcall(omsi.plugin.read, path)
    if not ok or type(content) ~= "string" or content == "" then return nil end
    local parsed_ok, result = pcall(omsi.json.decode, content)
    if parsed_ok and type(result) == "table" then return result end
    return nil
end

local function normalize_name(value)
    local s = safe_string(value)
    s = s:gsub("\194\160", " ")
    -- string.lower handles Latin; Lua's default locale-independent lower does not
    -- fold Cyrillic, so fold the common Cyrillic uppercase alphabet explicitly.
    local upper = {"А","Б","В","Г","Д","Е","Ё","Ж","З","И","Й","К","Л","М","Н","О","П","Р","С","Т","У","Ф","Х","Ц","Ч","Ш","Щ","Ъ","Ы","Ь","Э","Ю","Я"}
    local lower = {"а","б","в","г","д","е","ё","ж","з","и","й","к","л","м","н","о","п","р","с","т","у","ф","х","ц","ч","ш","щ","ъ","ы","ь","э","ю","я"}
    for i = 1, #upper do s = s:gsub(upper[i], lower[i]) end
    s = string.lower(s)
    s = s:gsub("^%s+", ""):gsub("%s+$", "")
    s = s:gsub("%s+", " ")
    return s
end

local function list_names(stops)
    local out = {}
    if type(stops) ~= "table" then return out end
    for _, stop in ipairs(stops) do
        if type(stop) == "table" then
            out[#out + 1] = safe_string(stop.name)
        else
            out[#out + 1] = safe_string(stop)
        end
    end
    return out
end

local function same_stop_lists(a, b)
    if type(a) ~= "table" or type(b) ~= "table" or #a ~= #b or #a == 0 then return false end
    for i = 1, #a do
        if normalize_name(a[i]) ~= normalize_name(b[i]) then return false end
    end
    return true
end

local function get_duty_state()
    local duty = nil
    local ok, value = pcall(omsi.duty.get)
    if ok and type(value) == "table" then duty = value end
    local stops = {}
    local stop_ok, stop_value = pcall(omsi.duty.stops)
    if stop_ok and type(stop_value) == "table" then stops = stop_value end
    return duty, stops
end

local function find_next_stop_index(stops, duty)
    if type(stops) == "table" then
        for i, stop in ipairs(stops) do
            if type(stop) == "table" and stop.passed == false then return i end
        end
    end
    local next_stop = type(duty) == "table" and duty.next_stop or nil
    if type(next_stop) == "table" and type(stops) == "table" then
        for i, stop in ipairs(stops) do
            if type(stop) == "table" then
                if next_stop.id ~= nil and stop.id == next_stop.id then return i end
                if normalize_name(next_stop.name) ~= "" and normalize_name(stop.name) == normalize_name(next_stop.name) then return i end
            end
        end
    end
    if type(duty) == "table" and type(duty.stops) == "table" then
        local idx = tonumber(duty.stops.next_stop_index)
        if idx and idx >= 1 and idx <= #stops then return math.floor(idx) end
    end
    if #stops > 0 then return 1 end
    return nil
end

local function bus_position()
    local ok, x, y, z, heading = pcall(omsi.position)
    if not ok or type(x) ~= "number" or type(y) ~= "number" or type(z) ~= "number" then
        return nil
    end
    return x, y, z, tonumber(heading) or 0
end

local function ceiling_position(options)
    local x, y, z, heading = bus_position()
    if not x then return nil end
    options = type(options) == "table" and options or {}
    local forward = clamp(options.forward, -8, 8, tonumber(settings.speaker_forward) or 0.2)
    local right = clamp(options.right, -8, 8, tonumber(settings.speaker_right) or 0.0)
    local up = clamp(options.up, -2, 8, tonumber(settings.speaker_up) or 2.25)

    -- Some openOMSI builds keep the audio listener tied to the bus rather than the
    -- manually turned head. In the cab, counter-rotate the point source by the head's
    -- yaw relative to the vehicle so turning to the right makes a fixed cabin speaker
    -- come from the left (and vice versa). The camera itself remains the true listener
    -- position; this only compensates orientation. Outside views keep world-fixed sources.
    local in_cab = false
    local cab_ok, cab_value = pcall(function() return omsi.camera.in_cab() end)
    if cab_ok and cab_value == true then in_cab = true end
    if in_cab then
        local head_delta = nil
        -- look_yaw is already the driver/passenger head turn relative to the bus.
        -- Prefer it over absolute camera yaw; some camera modes expose a world yaw
        -- which does not change consistently with mouse-look.
        local get_ok, camera = pcall(function() return omsi.camera.get() end)
        if get_ok and type(camera) == "table" and type(camera.look_yaw) == "number" then
            head_delta = camera.look_yaw
        end
        if head_delta == nil then
            local yaw_ok, camera_yaw = pcall(function() return omsi.camera.orientation() end)
            if yaw_ok and type(camera_yaw) == "number" then
                head_delta = (camera_yaw - heading + 540.0) % 360.0 - 180.0
            elseif get_ok and type(camera) == "table" and type(camera.yaw) == "number" then
                head_delta = (camera.yaw - heading + 540.0) % 360.0 - 180.0
            end
        end
        if head_delta ~= nil then
            head_delta = (head_delta + 540.0) % 360.0 - 180.0
            -- Mild compensation rather than a full counter-rotation: the native
            -- mixer may already follow some camera orientation, so this is capped
            -- at 60% to make head-turn panning audible without snapping to hard L/R.
            if math.abs(head_delta) <= 135.0 then
                local d = math.rad(head_delta * 0.60)
                local c, sn = math.cos(d), math.sin(d)
                local adjusted_forward = forward * c + right * sn
                local adjusted_right = right * c - forward * sn
                forward, right = adjusted_forward, adjusted_right
            end
        end
    end

    local rad = math.rad(heading)
    local sin_h, cos_h = math.sin(rad), math.cos(rad)
    -- Position uses x east, y north, z up; heading clockwise from north.
    local px = x + sin_h * forward + cos_h * right
    local py = y + cos_h * forward - sin_h * right
    local pz = z + up
    return px, py, pz
end

local function stop_active(reason, report)
    local had_sound = active_sound_id ~= nil or active_outside_sound_id ~= nil
    if active_sound_id ~= nil then pcall(omsi.audio.stop, active_sound_id) end
    if active_outside_sound_id ~= nil then pcall(omsi.audio.stop, active_outside_sound_id) end
    if had_sound and report ~= false and active_command_id then
        send_event({ type = "stopped", id = active_command_id, reason = reason or "stopped" })
    end
    active_sound_id = nil
    active_outside_sound_id = nil
    active_command_id = nil
    active_source = nil
    active_options = nil
    active_then_close_doors = false
    active_base_volume = 0.9
    active_exterior_volume = tonumber(settings.exterior_volume) or 0.25
    active_current_volume = 0
    active_outside_current_volume = 0
    doppler_history.informant = nil
end

local function camera_is_inside()
    -- `camera.in_cab()` is the documented API. Get/view are safe fallbacks for
    -- API variants and brief failures while switching camera modes.
    local ok, value = pcall(function() return omsi.camera.in_cab() end)
    if ok and type(value) == "boolean" then return value end

    local get_ok, camera = pcall(function() return omsi.camera.get() end)
    if get_ok and type(camera) == "table" and type(camera.in_cab) == "boolean" then
        return camera.in_cab
    end

    local view_ok, view = pcall(function() return omsi.camera.view() end)
    if view_ok and type(view) == "string" then
        return view == "driver" or view == "pax"
    end
    return false
end

local function camera_xyz()
    local ok, x, y, z = pcall(function() return omsi.camera.position() end)
    if ok and type(x) == "number" and type(y) == "number" and type(z) == "number" then
        return x, y, z
    end
    local get_ok, camera = pcall(function() return omsi.camera.get() end)
    if get_ok and type(camera) == "table" and type(camera.x) == "number" then
        return camera.x, camera.y or 0, camera.z or 0
    end
    return nil
end

local function source_xyz(source, options)
    if type(options) ~= "table" then return nil end
    if source == "ceiling" then
        return ceiling_position({forward = options.forward, right = options.right, up = options.up})
    elseif source == "fixed" then
        return tonumber(options.x), tonumber(options.y), tonumber(options.z)
    else
        local ok, x, y, z = pcall(function() return omsi.position() end)
        if ok and type(x) == "number" and type(y) == "number" and type(z) == "number" then
            return x, y, z
        end
    end
    return nil
end

local function camera_distance_from_source(source, options)
    local cx, cy, cz = camera_xyz()
    local sx, sy, sz = source_xyz(source, options)
    if not cx or not sx then return nil end
    local dx, dy, dz = cx - sx, cy - sy, cz - sz
    return math.sqrt(dx * dx + dy * dy + dz * dz), cx, cy, cz, sx, sy, sz
end

local function doppler_pitch(source, options, now, history_key)
    -- openOMSI's native mixer already applies Doppler to spatial sources. The previous
    -- hand-rolled pitch controller estimated velocity by differencing camera/world
    -- positions and then multiplied the native result, causing exaggerated "waaaoow"
    -- pitch swings. Keep the clip pitch neutral and let the engine supply the single,
    -- physically consistent Doppler calculation.
    return 1.0
end

local function start_play(command)
    if type(command.file) ~= "string" or command.file == "" then
        send_event({ type = "error", id = command.id, message = "command has no interior WAV file" })
        return
    end
    stop_active("replaced")

    local source = tostring(command.source or settings.source_mode or "ceiling")
    local opts = {
        volume = clamp(command.volume, 0, 1, 0.9),
        range = clamp(command.range, 0.5, 100, tonumber(settings.range) or 7.0),
        loop = false,
    }
    if source == "bus" then
        opts.on_bus = true
    elseif source == "fixed" then
        opts.x = tonumber(command.x) or 0
        opts.y = tonumber(command.y) or 0
        opts.z = tonumber(command.z) or 0
    else
        source = "ceiling"
        local px, py, pz = ceiling_position({
            forward = tonumber(command.forward),
            right = tonumber(command.right),
            up = tonumber(command.up),
        })
        if not px then
            send_event({ type = "error", id = command.id,
                         message = "no bus position (enter a bus before playing spatial audio)" })
            return
        end
        opts.x, opts.y, opts.z = px, py, pz
    end

    active_command_base_volume = clamp(command.volume, 0, 1, 0.9)
    active_base_volume = active_command_base_volume * informant_output_level
    active_exterior_volume = clamp(command.exterior_volume, 0, 1.0, tonumber(settings.exterior_volume) or 0.25)
    local inside = camera_is_inside()
    active_current_volume = inside and active_base_volume or math.max(MIN_LIVE_VOLUME, active_base_volume * MIN_LIVE_VOLUME)
    active_outside_current_volume = inside and math.max(MIN_LIVE_VOLUME, active_base_volume * MIN_LIVE_VOLUME)
        or math.max(MIN_LIVE_VOLUME, active_base_volume * active_exterior_volume)
    opts.volume = active_current_volume
    local ok, sound_id, reason = pcall(omsi.audio.play, command.file, opts)
    if not ok or sound_id == nil then
        send_event({ type = "error", id = command.id, message = tostring(reason or sound_id or "audio.play failed") })
        return
    end
    active_sound_id = sound_id
    active_command_id = command.id
    active_source = source
    active_then_close_doors = command.then_close_doors == true
    active_options = {
        forward = tonumber(command.forward),
        right = tonumber(command.right),
        up = tonumber(command.up),
        x = opts.x, y = opts.y, z = opts.z,
        range = opts.range,
        exterior_range = clamp(tonumber(command.exterior_range), 2.0, 100.0, settings.exterior_range),
    }

    -- A second, synchronized voice is already low-passed to ~1.45 kHz and mono.
    -- It is faded in only from an outside camera, while the clear cab variant is faded out.
    if type(command.outside_file) == "string" and command.outside_file ~= "" then
        local outside_opts = {}
        for key, value in pairs(opts) do outside_opts[key] = value end
        outside_opts.volume = active_outside_current_volume
        -- Further reduce how far the already-quiet exterior murmur carries.
        -- Exterior has its own useful radius. A tiny multiplier here used to make the
        -- sound disappear completely as soon as the camera moved outside the bus.
        outside_opts.range = clamp(tonumber(command.exterior_range), 2.0, 100.0, settings.exterior_range)
        local outside_ok, outside_id, outside_reason = pcall(omsi.audio.play, command.outside_file, outside_opts)
        if outside_ok and outside_id ~= nil then
            active_outside_sound_id = outside_id
        else
            active_outside_sound_id = nil
            -- If a legacy or damaged cache has no outside file, fall back to one very quiet voice.
            active_current_volume = inside and active_base_volume or math.max(MIN_LIVE_VOLUME, active_base_volume * active_exterior_volume)
            pcall(omsi.audio.set, active_sound_id, { volume = active_current_volume })
            send_event({ type = "audio_variant_warning", id = command.id,
                         message = "outside-muffled variant unavailable: " .. tostring(outside_reason or outside_id) })
        end
    end
    active_last_audio_update = omsi.time()
    send_event({ type = "started", id = command.id, sound_id = sound_id,
                 source = source, file = command.file,
                 source_x = active_options.x, source_y = active_options.y, source_z = active_options.z,
                 outside_variant = active_outside_sound_id ~= nil,
                 camera_inside = inside, exterior_scale = active_exterior_volume })
end

local function stop_music(reason, report, preserve_native_radio_state)
    -- An explicit stop/new track must also discard a queued radio segment.
    music_queued_command = nil
    local had_music = music_sound_id ~= nil or music_outside_sound_id ~= nil or native_radio ~= nil
    if native_radio ~= nil then
        -- When switching straight to another native station, keep the game variable
        -- live and overwrite it below. Writing 0 and the new index in one tick caused
        -- some buses to interpret the change as radio OFF and never reopen the stream.
        if not preserve_native_radio_state then
            if native_radio.variable == "SndExt_Radio" then pcall(omsi.set_var, "SndExt_Radio", 0) end
            if native_radio.variable == "Snd_Radio" then pcall(omsi.set_var, "Snd_Radio", 0) end
        end
        native_radio = nil
    end
    native_radio_zero_since = nil
    if music_sound_id ~= nil then pcall(omsi.audio.stop, music_sound_id) end
    if music_outside_sound_id ~= nil then pcall(omsi.audio.stop, music_outside_sound_id) end
    local finished_id = music_command_id
    local track = music_track_name
    music_sound_id = nil
    music_outside_sound_id = nil
    music_command_id = nil
    music_track_name = nil
    music_source = nil
    music_options = nil
    music_command_base_volume = 0.55
    music_base_volume = 0.55 * music_output_level
    music_exterior_volume = tonumber(settings.music_exterior_volume) or 0.30
    music_current_volume = 0
    music_outside_current_volume = 0
    doppler_history.music = nil
    if had_music and report ~= false then
        send_event({ type = "music_stopped", id = finished_id, track = track, reason = reason or "stopped" })
    end
end

local function start_music(command)
    if type(command.file) ~= "string" or command.file == "" then
        send_event({ type = "music_error", id = command.id, message = "music command has no WAV file" })
        return
    end
    stop_music("replaced", false)
    local source = tostring(command.source or settings.source_mode or "ceiling")
    local opts = { volume = clamp(command.volume, 0, 1, 0.55), range = clamp(command.range, 0.5, 100, tonumber(settings.range) or 7.0), loop = false }
    if source == "bus" then
        opts.on_bus = true
    elseif source == "fixed" then
        opts.x, opts.y, opts.z = tonumber(command.x) or 0, tonumber(command.y) or 0, tonumber(command.z) or 0
    else
        source = "ceiling"
        local px, py, pz = ceiling_position({forward = tonumber(command.forward), right = tonumber(command.right), up = tonumber(command.up)})
        if not px then
            send_event({ type = "music_error", id = command.id, message = "no bus position (enter a bus before playing spatial music)" })
            return
        end
        opts.x, opts.y, opts.z = px, py, pz
    end
    music_command_base_volume = clamp(command.volume, 0, 1, 0.55)
    music_base_volume = music_command_base_volume * music_output_level
    music_exterior_volume = clamp(command.exterior_volume, 0, 1.0, tonumber(settings.music_exterior_volume) or 0.30)
    local inside = camera_is_inside()
    music_current_volume = inside and music_base_volume or math.max(MIN_LIVE_VOLUME, music_base_volume * MIN_LIVE_VOLUME)
    music_outside_current_volume = inside and math.max(MIN_LIVE_VOLUME, music_base_volume * MIN_LIVE_VOLUME)
        or math.max(MIN_LIVE_VOLUME, music_base_volume * music_exterior_volume)
    opts.volume = music_current_volume
    local ok, sound_id, reason = pcall(omsi.audio.play, command.file, opts)
    if not ok or sound_id == nil then
        send_event({ type = "music_error", id = command.id, message = tostring(reason or sound_id or "audio.play failed") })
        return
    end
    music_sound_id = sound_id
    music_command_id = command.id
    music_track_name = tostring(command.track or command.file)
    music_source = source
    music_options = {
        forward = tonumber(command.forward), right = tonumber(command.right), up = tonumber(command.up),
        x = opts.x, y = opts.y, z = opts.z, range = opts.range,
        exterior_range = clamp(tonumber(command.exterior_range), 2.0, 100.0, settings.exterior_range),
        source = source,
    }
    if type(command.outside_file) == "string" and command.outside_file ~= "" then
        local outside_opts = {}
        for key, value in pairs(opts) do outside_opts[key] = value end
        outside_opts.volume = music_outside_current_volume
        outside_opts.range = clamp(tonumber(command.exterior_range), 2.0, 100.0, settings.exterior_range)
        local outside_ok, outside_id = pcall(omsi.audio.play, command.outside_file, outside_opts)
        music_outside_sound_id = (outside_ok and outside_id ~= nil) and outside_id or nil
    end
    music_last_audio_update = omsi.time()
    send_event({ type = "music_started", id = command.id, track = music_track_name, source = source, outside_variant = music_outside_sound_id ~= nil })
end

local function update_music_sound()
    if music_sound_id == nil then return end
    if music_source == "ceiling" and music_options then
        local px, py, pz = ceiling_position(music_options)
        if px then
            pcall(omsi.audio.set, music_sound_id, {x = px, y = py, z = pz})
            if music_outside_sound_id ~= nil then pcall(omsi.audio.set, music_outside_sound_id, {x = px, y = py, z = pz}) end
        end
    end
    local inside = camera_is_inside()
    local distance = camera_distance_from_source(music_source, music_options)
    local outside_too_far = (not inside) and distance ~= nil
        and distance > (tonumber(music_options and music_options.exterior_range) or tonumber(settings.exterior_range) or 45.0)
    local pitch = doppler_pitch(music_source, music_options, omsi.time(), "music")
    local target_inside, target_outside
    if music_outside_sound_id ~= nil then
        target_inside = inside and music_base_volume or math.max(MIN_LIVE_VOLUME, music_base_volume * MIN_LIVE_VOLUME)
        target_outside = (not inside) and math.max(MIN_LIVE_VOLUME, music_base_volume * music_exterior_volume)
            or math.max(MIN_LIVE_VOLUME, music_base_volume * MIN_LIVE_VOLUME)
    else
        target_inside = inside and music_base_volume or math.max(MIN_LIVE_VOLUME, music_base_volume * music_exterior_volume)
        target_outside = 0
    end
    if music_base_volume <= 0.000001 then
        target_inside, target_outside = 0.0, 0.0
    elseif outside_too_far then
        target_inside = 0.0
        target_outside = 0.0
    end
    local now = omsi.time()
    local dt = math.max(0, math.min(0.5, now - music_last_audio_update))
    music_last_audio_update = now
    local blend = math.min(1.0, dt * 7.0)
    music_current_volume = music_current_volume + (target_inside - music_current_volume) * blend
    music_outside_current_volume = music_outside_current_volume + (target_outside - music_outside_current_volume) * blend
    if music_base_volume <= 0.000001 then
        music_current_volume, music_outside_current_volume = 0.0, 0.0
    elseif outside_too_far then
        music_current_volume = 0.00000001
        music_outside_current_volume = 0.00000001
    end
    pcall(omsi.audio.set, music_sound_id, {volume = music_current_volume, pitch = pitch})
    if music_outside_sound_id ~= nil then pcall(omsi.audio.set, music_outside_sound_id, {volume = music_outside_current_volume, pitch = pitch}) end
    local playing_ok, playing = pcall(omsi.audio.playing, music_sound_id)
    if playing_ok and not playing then
        if music_outside_sound_id ~= nil then pcall(omsi.audio.stop, music_outside_sound_id) end
        local finished_id, track = music_command_id, music_track_name
        music_sound_id, music_outside_sound_id, music_command_id = nil, nil, nil
        music_track_name, music_source, music_options = nil, nil, nil
        music_current_volume, music_outside_current_volume = 0, 0
        send_event({type = "music_finished", id = finished_id, track = track})
        -- Start an already prepared next segment on this game frame. This avoids
        -- a Python->UDP round trip and conversion work at every radio boundary.
        local queued = music_queued_command
        music_queued_command = nil
        if type(queued) == "table" then start_music(queued) end
    end
end

local function apply_native_radio(send_started)
    if type(native_radio) ~= "table" then return false end
    local index = math.max(1, math.floor(tonumber(native_radio.index) or 1))
    local volume = clamp(tonumber(native_radio.volume), 0, 1, 0.55)
    local ext = omsi.var("SndExt_Radio")
    if ext ~= nil then
        local ok, result = pcall(omsi.set_var, "SndExt_Radio", index)
        if not ok or result ~= true then
            send_event({type = "music_error", id = native_radio.id,
                        source = "native_radio", message = "bus did not accept SndExt_Radio=" .. tostring(index)})
            native_radio = nil
            return false
        end
        if omsi.var("SndVol_Radio") ~= nil then pcall(omsi.set_var, "SndVol_Radio", volume) end
        native_radio.variable = "SndExt_Radio"
    elseif index == 1 and omsi.var("Snd_Radio") ~= nil then
        local ok, result = pcall(omsi.set_var, "Snd_Radio", 1)
        if not ok or result ~= true then
            send_event({type = "music_error", id = native_radio.id,
                        source = "native_radio", message = "bus did not accept Snd_Radio=1"})
            native_radio = nil
            return false
        end
        native_radio.variable = "Snd_Radio"
    else
        send_event({type = "music_error", id = native_radio.id,
                    source = "native_radio", message = "this bus has no compatible radio variable (SndExt_Radio / Snd_Radio)"})
        native_radio = nil
        return false
    end
    if send_started then
        send_event({type = "music_started", id = native_radio.id, track = native_radio.track,
                    source = "openOMSI native radio", station_index = index, native = true})
    end
    return true
end

local function apply_native_radio_output_level()
    if type(native_radio) ~= "table" or not native_radio.variable then return end
    local output = clamp(tonumber(music_output_level), 0, 1, 1.0)
    local base = clamp(tonumber(native_radio.base_volume), 0, 1, 0.55)
    native_radio.volume = base * output
    local muted = output <= 0.0001
    if native_radio.variable == "Snd_Radio" then
        if muted and not native_radio.output_muted then
            pcall(omsi.set_var, "Snd_Radio", 0)
            native_radio.output_muted = true
        elseif not muted and native_radio.output_muted then
            pcall(omsi.set_var, "Snd_Radio", 1)
            native_radio.output_muted = false
        end
    elseif native_radio.variable == "SndExt_Radio" then
        if omsi.var("SndVol_Radio") ~= nil then
            pcall(omsi.set_var, "SndVol_Radio", native_radio.volume)
            native_radio.output_muted = muted
        elseif muted and not native_radio.output_muted then
            pcall(omsi.set_var, "SndExt_Radio", 0)
            native_radio.output_muted = true
        elseif not muted and native_radio.output_muted then
            pcall(omsi.set_var, "SndExt_Radio", native_radio.index)
            native_radio.output_muted = false
        end
    end
end

local function start_native_radio(command)
    stop_music("replaced", false, true)
    local index = tonumber(command.station_index)
    if not index or index < 1 then
        send_event({type = "music_error", id = command.id, source = "native_radio", message = "invalid radio station index"})
        return
    end
    native_radio_zero_since = nil
    native_radio = {
        id = command.id, index = math.floor(index),
        base_volume = clamp(tonumber(command.volume), 0, 1, 0.55),
        volume = clamp(tonumber(command.volume), 0, 1, 0.55) * music_output_level,
        track = tostring(command.track or "Internet radio"),
        variable = nil,
        output_muted = false,
    }
    native_radio_last_apply = omsi.time()
    apply_native_radio(true)
    apply_native_radio_output_level()
end

local function update_native_radio()
    if type(native_radio) ~= "table" or not native_radio.variable then return end
    -- Do not keep writing our station every 0.75 s: Shift+R is openOMSI's own list
    -- navigation and must be allowed to change the variable without this plugin racing it.
    -- Observe the variable and report the game's choice back to the companion UI.
    local value = tonumber(omsi.var(native_radio.variable))
    if value == nil then return end
    if native_radio.output_muted and value <= 0 then return end
    local selected = math.floor(value + 0.5)
    -- openOMSI may momentarily write zero while Shift+R advances through its station
    -- list. Do not treat that transient value as radio OFF until it stays zero long
    -- enough to represent a deliberate user action.
    if selected <= 0 then
        if native_radio_zero_since == nil then
            native_radio_zero_since = omsi.time()
            return
        end
        if omsi.time() - native_radio_zero_since < 0.8 then return end
        local old = native_radio
        native_radio = nil
        native_radio_zero_since = nil
        send_event({type = "music_stopped", id = old.id, track = old.track,
                    source = "native_radio", reason = "radio switched off in game"})
        return
    end
    native_radio_zero_since = nil
    if native_radio.variable == "Snd_Radio" then
        return -- Snd_Radio is only an on/off switch; it does not expose station number.
    end
    if selected == native_radio.index then return end
    local old = native_radio
    native_radio.index = selected
    native_radio.track = "openOMSI radio #" .. tostring(selected)
    send_event({type = "native_radio_station_changed", id = old.id,
                station_index = selected, source = "native_radio"})
end

local function process_music_file()
    local command = read_json_file(MUSIC_FILE)
    if type(command) ~= "table" or type(command.id) ~= "string" or command.id == last_music_command_id then return end
    last_music_command_id = command.id
    if command.action == "native_radio" then
        start_native_radio(command)
    elseif command.action == "play_music" then
        start_music(command)
    elseif command.action == "queue_music" then
        -- Keep one successor only; the companion feeds the next one once this
        -- segment reports music_started. Never replace a live source mid-track.
        if music_sound_id ~= nil then
            music_queued_command = command
            send_event({type = "music_queued", id = command.id, track = tostring(command.track or command.file)})
        else
            start_music(command)
        end
    elseif command.action == "stop_music" then
        stop_music("requested")
    end
end

local function process_command(command)
    if type(command) ~= "table" or type(command.id) ~= "string" then return end
    if command.id == last_command_id then return end
    last_command_id = command.id
    local action = command.action
    if action == "play" then
        start_play(command)
    elseif action == "set_output_levels" then
        informant_output_level = clamp(tonumber(command.informant_level), 0, 1, informant_output_level)
        music_output_level = clamp(tonumber(command.music_level), 0, 1, music_output_level)
        active_base_volume = active_command_base_volume * informant_output_level
        music_base_volume = music_command_base_volume * music_output_level
        if type(native_radio) == "table" then
            native_radio.volume = (tonumber(native_radio.base_volume) or 0.55) * music_output_level
            if native_radio.variable == "Snd_Radio" then
                local should_mute = music_output_level <= 0.0001
                if should_mute and not native_radio.output_muted then
                    pcall(omsi.set_var, "Snd_Radio", 0)
                    native_radio.output_muted = true
                elseif not should_mute and native_radio.output_muted then
                    pcall(omsi.set_var, "Snd_Radio", 1)
                    native_radio.output_muted = false
                end
            elseif native_radio.variable == "SndExt_Radio" then
                if omsi.var("SndVol_Radio") ~= nil then
                    apply_native_radio(false)
                else
                    local should_mute = music_output_level <= 0.0001
                    if should_mute and not native_radio.output_muted then
                        pcall(omsi.set_var, "SndExt_Radio", 0)
                        native_radio.output_muted = true
                    elseif not should_mute and native_radio.output_muted then
                        pcall(omsi.set_var, "SndExt_Radio", native_radio.index)
                        native_radio.output_muted = false
                    end
                end
            end
        end
        send_event({ type = "volume_levels_applied", informant_level = informant_output_level,
                     music_level = music_output_level })
    elseif action == "stop" then
        stop_active("requested")
        send_event({ type = "stop_ack", id = command.id })
    elseif action == "noop" then
        -- Invalidate any old command file when the bridge starts.
    elseif action == "ping" then
        send_event({ type = "pong", id = command.id })
    else
        send_event({ type = "error", id = command.id, message = "unknown action: " .. tostring(action) })
    end
end

local function find_matching_timetable_trip(route_stops, duty)
    local ok_lines, lines = pcall(omsi.timetable.lines)
    if not ok_lines or type(lines) ~= "table" then return nil, "timetable lines unavailable" end
    local matches = {}
    local seen_groups = {}
    local current_line = type(duty) == "table" and safe_string(duty.line) or ""
    local current_tour = type(duty) == "table" and safe_string(duty.tour) or ""

    for _, line in ipairs(lines) do
        if type(line) == "table" and type(line.name) == "string" and type(line.tours) == "table" then
            for _, tour in ipairs(line.tours) do
                if type(tour) == "table" then
                    local tour_name = safe_string(tour.number)
                    local trip_count = (type(tour.trips) == "table" and #tour.trips) or 0
                    if trip_count < 1 then trip_count = 1 end
                    for trip_index = 1, trip_count do
                        local stops_ok, trip_stops = pcall(omsi.timetable.stops, line.name, tour_name, trip_index)
                        if stops_ok and type(trip_stops) == "table" then
                            local names = list_names(trip_stops)
                            if same_stop_lists(route_stops, names) then
                                local group_key = line.name .. "\0" .. tour_name
                                if not seen_groups[group_key] then
                                    seen_groups[group_key] = true
                                    matches[#matches + 1] = {
                                        line = line.name,
                                        tour = tour_name,
                                        trip = trip_index,
                                        current = (line.name == current_line and tour_name == current_tour),
                                    }
                                end
                                break -- one matching trip is enough for a line/tour pattern
                            end
                        end
                    end
                end
            end
        end
    end

    if #matches == 0 then return nil, "no timetable trip has this exact stop sequence" end
    for _, candidate in ipairs(matches) do
        if candidate.current then return candidate, "current duty belongs to the matching line and tour" end
    end
    if #matches == 1 then return matches[1], "unique exact timetable match" end
    return nil, "the stop sequence matches multiple lines/tours; not switching the duty automatically"
end

local function current_duty_matches_app_route()
    if type(app_route.stops) ~= "table" or #app_route.stops == 0 then return false end
    local _, stops = get_duty_state()
    return same_stop_lists(app_route.stops, list_names(stops))
end

local function process_route(route)
    if type(route) ~= "table" or type(route.id) ~= "string" or route.id == last_route_id then return end
    last_route_id = route.id
    if route.action ~= "set_route" then return end
    app_route = {
        map = safe_string(route.map),
        route = safe_string(route.route),
        stops = type(route.stops) == "table" and route.stops or {},
    }
    route_pending_trip = nil
    route_pending_until = -1
    last_nav_target_index = nil

    local duty, _ = get_duty_state()
    if current_duty_matches_app_route() then
        route_mode = "auto"
        route_reason = "selected route exactly matches the current openOMSI duty"
        send_event({ type = "route_status", mode = route_mode, map = app_route.map,
                     route = app_route.route, message = route_reason })
        return
    end

    if settings.auto_start_matching_trip and #app_route.stops > 0 then
        local candidate, reason = find_matching_timetable_trip(app_route.stops, duty)
        if candidate then
            local call_ok, started, start_reason = pcall(
                omsi.duty.start, candidate.line, candidate.tour, candidate.trip, 1
            )
            if call_ok and started then
                route_mode = "pending"
                route_reason = "matching duty requested; waiting for openOMSI to publish its stops"
                route_pending_trip = candidate
                route_pending_until = omsi.time() + 4.0
                send_event({ type = "route_status", mode = route_mode, map = app_route.map,
                             route = app_route.route, line = candidate.line, tour = candidate.tour,
                             trip = candidate.trip, message = route_reason })
                return
            else
                route_reason = "duty.start failed: " .. tostring(start_reason or started)
            end
        else
            route_reason = reason or "no exact timetable match"
        end
    else
        route_reason = "selected app route does not exactly match current duty stops"
    end

    route_mode = "manual"
    send_event({ type = "route_status", mode = route_mode, map = app_route.map,
                 route = app_route.route, message = route_reason })
end

local function find_name_match(name, stops)
    local wanted = normalize_name(name)
    if wanted == "" or type(stops) ~= "table" then return nil end
    local match = nil
    for i, stop in ipairs(stops) do
        local stop_name = type(stop) == "table" and stop.name or stop
        if normalize_name(stop_name) == wanted then
            if match ~= nil then return nil end -- repeated names are ambiguous
            match = i
        end
    end
    return match
end

local function process_navigation(nav)
    if type(nav) ~= "table" or type(nav.id) ~= "string" or nav.id == last_nav_id then return end
    last_nav_id = nav.id
    if nav.action ~= "navigate" then return end
    if not settings.allow_manual_ibis_sync and route_mode ~= "auto" then
        send_event({ type = "nav_ack", id = nav.id, ok = false, mode = route_mode,
                     message = "manual IBIS sync is disabled in settings" })
        return
    end

    local duty, stops = get_duty_state()
    if #stops == 0 then
        send_event({ type = "nav_ack", id = nav.id, ok = false, mode = route_mode,
                     message = "no active IBIS duty / timetable trip" })
        return
    end

    local target_index = math.floor(tonumber(nav.target_index) or 0) + 1
    if target_index < 1 then target_index = 1 end
    if target_index > #stops then target_index = #stops end
    local desired = nil
    local message = ""
    local delta = math.floor(tonumber(nav.delta) or 0)
    local target_delta = math.floor(tonumber(nav.target_delta) or delta)
    local step_delta = math.floor(tonumber(nav.step_delta) or delta)

    if route_mode == "auto" and same_stop_lists(app_route.stops, list_names(stops)) then
        desired = target_index
        message = "exact route match: IBIS moved to the selected stop"
    else
        -- A hand-written route may use different names/order from the game timetable.
        -- Prefer a unique name match. If the app changed only arrival/departure phase
        -- (same target index), but that name points at the currently selected IBIS stop,
        -- still honour a manual Next/Back button press by one stop in that direction.
        desired = find_name_match(nav.target_name, stops)
        local current = find_next_stop_index(stops, duty) or 1
        if desired then
            if target_delta == 0 and step_delta ~= 0 and desired == current and settings.allow_manual_ibis_sync then
                desired = current + (step_delta > 0 and 1 or -1)
                if desired < 1 then desired = 1 end
                if desired > #stops then desired = #stops end
                message = "manual route: same app target; honoured IBIS step direction"
            else
                message = "matched selected stop by name"
            end
        elseif settings.allow_manual_ibis_sync then
            if delta ~= 0 then
                desired = current + delta
                if desired < 1 then desired = 1 end
                if desired > #stops then desired = #stops end
                message = "manual route: moved IBIS by the same step direction"
            else
                message = "manual route: no stop-name match; no IBIS movement for this first position"
            end
        else
            message = "route mismatch and manual IBIS sync is disabled"
        end
    end

    if not desired then
        send_event({ type = "nav_ack", id = nav.id, ok = true, moved = false,
                     mode = route_mode, message = message })
        return
    end

    local call_ok, result = pcall(omsi.duty.skip_to, desired)
    if not call_ok or result ~= true then
        send_event({ type = "nav_ack", id = nav.id, ok = false, moved = false,
                     mode = route_mode, message = "duty.skip_to failed: " .. tostring(result) })
        return
    end

    last_nav_target_index = target_index
    nav_echo_pending = true
    send_event({ type = "nav_ack", id = nav.id, ok = true, moved = true,
                 ibIs_next_index = desired, mode = route_mode, message = message })
end

local function apply_settings(value)
    if type(value) ~= "table" then return end
    settings.exterior_volume = clamp(tonumber(value.exterior_volume), 0, 1.0, settings.exterior_volume)
    settings.music_exterior_volume = clamp(tonumber(value.music_exterior_volume), 0, 1.0, settings.music_exterior_volume)
    settings.exterior_range_factor = clamp(tonumber(value.exterior_range_factor), 0.20, 1.0, settings.exterior_range_factor)
    settings.exterior_range = clamp(tonumber(value.exterior_range), 2.0, 100.0, settings.exterior_range)
    settings.auto_start_matching_trip = value.auto_start_matching_trip == true
    settings.follow_game_progress = value.follow_game_progress ~= false
    settings.allow_manual_ibis_sync = value.allow_manual_ibis_sync ~= false
    settings.range = clamp(tonumber(value.range), 0.5, 100, settings.range)
    if value.source_mode == "ceiling" or value.source_mode == "bus" or value.source_mode == "fixed" then
        settings.source_mode = value.source_mode
    end
    settings.speaker_forward = clamp(tonumber(value.speaker_forward), -8, 8, settings.speaker_forward)
    settings.speaker_right = clamp(tonumber(value.speaker_right), -8, 8, settings.speaker_right)
    settings.speaker_up = clamp(tonumber(value.speaker_up), -2, 8, settings.speaker_up)

    informant_output_level = clamp(tonumber(value.informant_output_level), 0, 1, informant_output_level)
    music_output_level = clamp(tonumber(value.music_output_level), 0, 1, music_output_level)
    active_base_volume = active_command_base_volume * informant_output_level
    music_base_volume = music_command_base_volume * music_output_level
    apply_native_radio_output_level()
end

local function process_settings_file()
    local value = read_json_file(SETTINGS_FILE)
    if not value or type(value.id) ~= "string" or value.id == last_settings_id then return end
    last_settings_id = value.id
    apply_settings(value)
    send_event({ type = "control_status", message = "settings applied", speaker_effect = tonumber(value.speaker_effect) or 45,
                 exterior_volume = settings.exterior_volume, music_exterior_volume = settings.music_exterior_volume })
end

local function process_command_file()
    process_command(read_json_file(COMMAND_FILE))
end

local function process_route_file()
    process_route(read_json_file(ROUTE_FILE))
end

local function process_nav_file()
    process_navigation(read_json_file(NAV_FILE))
end

local function update_active_sound()
    if active_sound_id == nil then return end
    if active_source == "ceiling" and active_options then
        local px, py, pz = ceiling_position(active_options)
        if px then
            pcall(omsi.audio.set, active_sound_id, { x = px, y = py, z = pz })
            if active_outside_sound_id ~= nil then
                pcall(omsi.audio.set, active_outside_sound_id, { x = px, y = py, z = pz })
            end
        end
    elseif active_source == "fixed" and active_options then
        -- Fixed sources stay where the command put them.
    end

    local inside = camera_is_inside()
    local distance = camera_distance_from_source(active_source, active_options)
    local outside_too_far = (not inside) and distance ~= nil
        and distance > (tonumber(active_options and active_options.exterior_range) or tonumber(settings.exterior_range) or 45.0)
    local pitch = doppler_pitch(active_source, active_options, omsi.time(), "informant")
    local target_inside
    local target_outside = 0.0
    if active_outside_sound_id ~= nil then
        -- Do not set either stream to a literal zero while it is still needed for the
        -- crossfade. Some audio backends treat a zero-volume source as stopped, which
        -- caused the opposite camera's stream not to return during the same phrase.
        target_inside = inside and active_base_volume or math.max(MIN_LIVE_VOLUME, active_base_volume * MIN_LIVE_VOLUME)
        target_outside = (not inside) and math.max(MIN_LIVE_VOLUME, active_base_volume * active_exterior_volume)
            or math.max(MIN_LIVE_VOLUME, active_base_volume * MIN_LIVE_VOLUME)
    else
        -- Graceful fallback if the muffled cache variant could not be played.
        target_inside = inside and active_base_volume or math.max(MIN_LIVE_VOLUME, active_base_volume * active_exterior_volume)
    end
    if active_base_volume <= 0.000001 then
        target_inside, target_outside = 0.0, 0.0
    elseif outside_too_far then
        target_inside = 0.0
        target_outside = 0.0
    end
    local now = omsi.time()
    local dt = math.max(0, math.min(0.5, now - active_last_audio_update))
    active_last_audio_update = now
    local blend = math.min(1.0, dt * 7.0)
    active_current_volume = active_current_volume + (target_inside - active_current_volume) * blend
    active_outside_current_volume = active_outside_current_volume + (target_outside - active_outside_current_volume) * blend
    if active_base_volume <= 0.000001 then
        active_current_volume, active_outside_current_volume = 0.0, 0.0
    elseif outside_too_far then
        active_current_volume = 0.00000001
        active_outside_current_volume = 0.00000001
    end

    pcall(omsi.audio.set, active_sound_id, { volume = active_current_volume, pitch = pitch })
    if active_outside_sound_id ~= nil then
        pcall(omsi.audio.set, active_outside_sound_id, { volume = active_outside_current_volume, pitch = pitch })
    end

    local ok, playing = pcall(omsi.audio.playing, active_sound_id)
    if ok and not playing then
        if active_outside_sound_id ~= nil then pcall(omsi.audio.stop, active_outside_sound_id) end
        local finished_id = active_command_id
        active_sound_id = nil
        active_outside_sound_id = nil
        active_command_id = nil
        active_source = nil
        active_options = nil
        local close_doors_after_sound = active_then_close_doors
        active_then_close_doors = false
        if close_doors_after_sound then
            local doors_ok, doors_open = pcall(omsi.bus.doors_open)
            if doors_ok and doors_open == true then
                local close_ok, close_result = pcall(omsi.bus.toggle_door)
                if not close_ok or close_result == false then
                    send_event({ type = "door_close_warning", message = "DOORS clip finished, but the current bus refused the door toggle" })
                end
            elseif not doors_ok or doors_open == nil then
                send_event({ type = "door_close_warning", message = "DOORS clip finished, but this bus/API did not report door state; doors were left unchanged" })
            end
        end
        if finished_id then send_event({ type = "finished", id = finished_id }) end
    end
end

local function update_game_state()
    local duty, stops = get_duty_state()
    local names = list_names(stops)
    if #app_route.stops > 0 then
        if same_stop_lists(app_route.stops, names) then
            route_mode = "auto"
            route_reason = "app stop sequence matches the current openOMSI duty"
            route_pending_trip = nil
            route_pending_until = -1
        elseif route_mode == "pending" and omsi.time() <= route_pending_until then
            -- Wait for duty.start() to publish the new trip.
        else
            if route_mode == "pending" then
                route_mode = "manual"
                route_reason = "openOMSI did not switch to the matching trip in time"
                send_event({ type = "route_status", mode = route_mode, map = app_route.map,
                             route = app_route.route, message = route_reason })
            elseif route_mode == "auto" then
                route_mode = "manual"
                route_reason = "the openOMSI duty no longer matches the selected app route"
                send_event({ type = "route_status", mode = route_mode, map = app_route.map,
                             route = app_route.route, message = route_reason })
            end
        end
    else
        route_mode = "manual"
        route_reason = "standalone AutoInformant follows the game duty directly"
    end

    local info = {}
    local info_ok, info_value = pcall(omsi.info)
    if info_ok and type(info_value) == "table" then info = info_value end

    local next_index = find_next_stop_index(stops, duty)
    local next_name = safe_string(info.next_stop)
    if next_name == "" and next_index and stops[next_index] and type(stops[next_index]) == "table" then
        next_name = safe_string(stops[next_index].name)
    elseif next_name == "" and type(duty) == "table" and type(duty.next_stop) == "table" then
        next_name = safe_string(duty.next_stop.name)
    end

    local stop_objects = {}
    for i, stop in ipairs(stops) do
        if type(stop) == "table" then
            stop_objects[#stop_objects + 1] = {
                raw_index = i,
                number = tonumber(stop.number) or i,
                id = stop.id,
                name = safe_string(stop.name),
                stops = stop.stops,
                closed = stop.closed == true,
                passed = stop.passed == true,
                arrival = tonumber(stop.arrival),
                departure = tonumber(stop.departure),
                x = tonumber(stop.x),
                y = tonumber(stop.y),
                z = tonumber(stop.z),
            }
        end
    end

    local camera_inside = camera_is_inside()
    local line = type(duty) == "table" and safe_string(duty.line) or safe_string(info.line)
    local tour = type(duty) == "table" and safe_string(duty.tour) or safe_string(info.tour)
    local trip = type(duty) == "table" and safe_string(duty.trip) or safe_string(info.trip)
    local trip_name = type(duty) == "table" and safe_string(duty.trip_name) or safe_string(info.trip_name)
    local duty_identity = line .. "\0" .. tour .. "\0" .. trip .. "\0" .. trip_name
    local duty_changed = last_duty_identity ~= nil and duty_identity ~= last_duty_identity
    last_duty_identity = duty_identity

    local next_stop_id = info.next_stop_id
    if next_stop_id == nil and next_index and stops[next_index] then next_stop_id = stops[next_index].id end
    local game_date = nil
    local date_ok, date_value = pcall(omsi.world.date)
    if date_ok and type(date_value) == "table" then game_date = date_value end
    local game_time = nil
    local time_ok, time_value = pcall(omsi.world.time)
    if time_ok then game_time = tonumber(time_value) end
    local event = {
        type = "game_state",
        version = 3,
        line = line,
        tour = tour,
        trip = trip,
        trip_name = trip_name,
        duty_changed = duty_changed,
        terminus = safe_string(type(duty) == "table" and duty.terminus or info.terminus),
        next_stop_index = tonumber(info.next_stop_number) or next_index or 0, -- one-based
        next_stop_name = next_name,
        next_stop_id = next_stop_id,
        next_stop_distance = tonumber(info.next_stop_distance),
        next_stop_distance_nav = tonumber(info.next_stop_distance_nav),
        previous_stop_name = safe_string(info.previous_stop),
        previous_stop_id = info.previous_stop_id,
        at_stop = (info.at_stop == true) or (type(duty) == "table" and duty.at_stop == true) or false,
        duty_stops = names,
        duty_stop_objects = stop_objects,
        sync_mode = route_mode,
        sync_reason = route_reason,
        app_map = app_route.map,
        app_route = app_route.route,
        camera_inside = camera_inside,
        radio_ext_available = has_bus_var("SndExt_Radio"),
        radio_simple_available = has_bus_var("Snd_Radio"),
        view = safe_string(info.view),
        map = safe_string(info.map),
        map_path = safe_string(info.map_path),
        speed = tonumber(info.speed) or 0,
        game_clock = tonumber(info.clock),
        game_time = game_time,
        game_date = game_date,
        paused = info.paused == true,
        vehicle_manufacturer = safe_string(info.vehicle_manufacturer),
        vehicle_model = safe_string(info.vehicle_model),
        destination = safe_string(info.destination),
        changed_by_app = nav_echo_pending,
    }
    send_game_state(event, stop_objects, names)
    nav_echo_pending = false
end

local function initialize_file_ids()
    local command = read_json_file(COMMAND_FILE)
    if command and type(command.id) == "string" then last_command_id = command.id end
    -- Do NOT mark the current app route as consumed at plugin startup. The app can
    -- launch before openOMSI; in that order the latest route JSON already exists when
    -- this plugin loads, and it must be processed once to perform initial sync.
    -- Navigation, unlike route selection, is intentionally not replayed after a reload.
    local nav = read_json_file(NAV_FILE)
    if nav and type(nav.id) == "string" then last_nav_id = nav.id end
    local value = read_json_file(SETTINGS_FILE)
    if value and type(value.id) == "string" then
        last_settings_id = value.id
        apply_settings(value)
    end
    -- Ignore only a stale music command already present when this plugin loads.
    -- Do not reset this ID during periodic settings refreshes: doing so can consume
    -- a fresh queued segment before process_music_file() sees it.
    local music = read_json_file(MUSIC_FILE)
    if music and type(music.id) == "string" then last_music_command_id = music.id end
end

initialize_file_ids()

omsi.on("start", function()
    omsi.log("AutoInformant Pro plugin v2 loaded; waiting for the companion app on UDP " .. tostring(BRIDGE_PORT))
    send_event({ type = "plugin_started", name = "AutoInformantPro", version = 2 })
end)

-- Q is forwarded to the app's existing act_next() function. The input API reports keys
-- but does not suppress the key from the bus, so disable a conflicting native Q action
-- if the bus would otherwise play a second announcement.
omsi.on("key", function(key, down)
    if key == "KeyQ" and down then send_event({ type = "hotkey", key = "Q" }) end
end)

-- The dedicated chord requests the map-configured DOORS.mp3 sequence. Unlike a normal
-- door key, it is not already bound to the bus: the clip can finish before toggling doors.
pcall(omsi.input.hotkey, "Ctrl+Alt+KeyD", function()
    send_event({ type = "door_close_request" })
end)
-- Shift+R belongs to openOMSI. Do not register a competing plugin hotkey: the native
-- radio list changes the same SndExt_Radio variable, and a second handler caused races.

omsi.every(POLL_SECONDS, function()
    poll_tick = poll_tick + 1
    process_command_file()
    process_music_file()
    update_native_radio()
    update_active_sound()
    update_music_sound()
    -- This standalone app follows openOMSI's active duty directly. It deliberately
    -- does not consume legacy bridge_route/bridge_nav files and never starts or skips a duty.
    if poll_tick % 10 == 0 then process_settings_file() end
    local now = omsi.time()
    if now - last_state_at >= STATE_INTERVAL then
        last_state_at = now
        update_game_state()
    end
    if now - last_ready_at >= 2.0 then
        last_ready_at = now
        send_event({ type = "ready", name = "AutoInformantPro", version = 2,
                     capabilities = { "live_output_levels", "category_audio_profiles" } })
    end
end)

omsi.on("stop", function()
    stop_active("plugin stopped", false)
    stop_music("plugin stopped", false)
end)
