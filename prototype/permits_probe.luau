--[[
	permits_probe.luau — investigates the serialized-but-unreadable property gap.

	Questions:
	  1. What do ReflectedProperty.Permits actually contain for readable vs
	     unreadable properties (i.e. what security boundary are we behind)?
	  2. Are any unreadable properties nevertheless WRITABLE from a session?
	  3. Does GetPropertiesOfClass with an elevated Security filter reveal more
	     (metadata-level elevation)?
	  4. What capability context does the session itself report?

	Read-only in effect: any property writes are to the task-local DataModel and
	are never persisted (no SavePlaceAsync).
]]

local results = { meta = { placeVersion = game.PlaceVersion } }

local function section(name: string, fn: () -> ())
	local ok, err = pcall(fn)
	if not ok then
		results[name] = { sectionError = tostring(err) }
	end
end

local function describe(v: any): string
	local t = typeof(v)
	local s = tostring(v)
	return t .. "|" .. s
end

------------------------------------------------------------------------------
-- 1+2. Permits + write-access sweep on Workspace's serialized properties
------------------------------------------------------------------------------
section("workspacePermits", function()
	local rs = game:GetService("ReflectionService")
	local props = rs:GetPropertiesOfClass("Workspace")
	local out = {}

	for _, p in props do
		if not p.Serialized then
			continue
		end
		local canRead, readValue = pcall(function()
			return (workspace :: any)[p.Name]
		end)

		local entry = {
			read = canRead,
			permitRead = p.Permits and describe(p.Permits.Read) or "nil",
			permitWrite = p.Permits and describe(p.Permits.Write) or "nil",
		}

		-- Write test: only for unreadable props (the interesting asymmetry).
		-- Write same-type junk; task DataModel is throwaway.
		if not canRead then
			local canWrite, writeErr = pcall(function()
				(workspace :: any)[p.Name] = (workspace :: any)[p.Name]
			end)
			-- self-assignment fails at the read; try a blind typed write instead
			if not canWrite then
				for _, guess in { 64, true, "probe" } do
					local ok2 = pcall(function()
						(workspace :: any)[p.Name] = guess
					end)
					if ok2 then
						canWrite = true
						break
					end
				end
			end
			entry.writable = canWrite
		end
		out[p.Name] = entry
	end
	results.workspacePermits = out
end)

------------------------------------------------------------------------------
-- 3. Does a Security filter on GetPropertiesOfClass change what's reported?
------------------------------------------------------------------------------
section("filterElevation", function()
	local rs = game:GetService("ReflectionService")
	local out = {}

	out.securityCapabilitiesGlobalExists = typeof(SecurityCapabilities) ~= "nil"

	local base = rs:GetPropertiesOfClass("Workspace")
	out.basePropCount = #base

	-- Try constructing SecurityCapabilities values and passing them as filter
	local attempts = {}
	local function tryFilter(label: string, make: () -> any)
		local okMake, securityValue = pcall(make)
		if not okMake then
			attempts[label] = "construct failed: " .. tostring(securityValue)
			return
		end
		local okCall, propsOrErr = pcall(function()
			return rs:GetPropertiesOfClass("Workspace", { Security = securityValue })
		end)
		attempts[label] = okCall and ("propCount=" .. #propsOrErr) or ("call failed: " .. tostring(propsOrErr))
	end

	tryFilter("fromNew", function()
		return (SecurityCapabilities :: any).new()
	end)
	tryFilter("fromEnumPlugin", function()
		return (SecurityCapabilities :: any).fromEnum(Enum.SecurityCapability.Plugin)
	end)
	tryFilter("enumDirect", function()
		return Enum.SecurityCapability.Plugin
	end)

	out.attempts = attempts

	-- What SecurityCapability enum items exist?
	local items = {}
	for _, item in Enum.SecurityCapability:GetEnumItems() do
		table.insert(items, item.Name)
	end
	out.securityCapabilityEnumItems = items

	results.filterElevation = out
end)

------------------------------------------------------------------------------
-- 4. What context does the session report?
------------------------------------------------------------------------------
section("sessionContext", function()
	local out = {}
	local RunService = game:GetService("RunService")
	for _, methodName in { "IsStudio", "IsServer", "IsClient", "IsEdit", "IsRunMode", "IsRunning" } do
		local ok, value = pcall(function()
			return (RunService :: any)[methodName](RunService)
		end)
		out[methodName] = ok and tostring(value) or "error"
	end

	-- Can we see plugin-ish globals?
	out.pluginGlobal = typeof(plugin)

	-- Spot-check a known PluginSecurity READ on another service for calibration
	local ok, err = pcall(function()
		return (game:GetService("Lighting") :: any).ExtendLightRangeTo120
	end)
	out.lightingExtendLightRangeRead = ok and "OK" or tostring(err)

	results.sessionContext = out
end)

return results
