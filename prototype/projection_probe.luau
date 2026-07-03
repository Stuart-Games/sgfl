--[[
	projection_probe.luau — runs inside an Open Cloud Luau Execution Session.

	Answers the open questions for the text-projection pipeline design:
	  1. Is ReflectionService available in execution sessions, and what does it report?
	  2. Which serialized properties are actually readable at this session's security level?
	  3. SerializationService behavior: non-creatable rejection (list roots), repeat-serialize
	     byte stability, output sizes.
	  4. Terrain: does CopyRegion(MaxExtents) work here, and can the TerrainRegion be serialized?
	  5. A trial deterministic text projection of small services, to eyeball fidelity and size.

	Everything is wrapped so one failing section never hides the others.
	The returned table comes back as the task's JSON output (4 MB cap — samples are truncated).
]]

local results = {
	meta = {
		placeId = game.PlaceId,
		gameId = game.GameId,
		placeVersion = game.PlaceVersion,
	},
}

local function section(name: string, fn: () -> ())
	local ok, err = pcall(fn)
	if not ok then
		results[name] = { sectionError = tostring(err) }
	end
end

-- Returns (true, value) or (false, "error string")
local function try(fn)
	local ok, result = pcall(fn)
	if ok then
		return true, result
	end
	return false, tostring(result)
end

------------------------------------------------------------------------------
-- 1. ReflectionService availability + shape
------------------------------------------------------------------------------
section("reflection", function()
	local rs = game:GetService("ReflectionService")
	local cls = rs:GetClass("Lighting")
	local props = rs:GetPropertiesOfClass("Lighting")

	local sample = {}
	for i, p in props do
		if i > 6 then
			break
		end
		table.insert(sample, {
			name = p.Name,
			owner = p.Owner,
			serialized = p.Serialized,
			type = tostring(p.Type),
		})
	end

	results.reflection = {
		available = true,
		lightingClassSerialized = cls and cls.Serialized,
		lightingSuperclass = cls and cls.Superclass,
		lightingPropCount = #props,
		sample = sample,
	}
end)

------------------------------------------------------------------------------
-- 2. Which serialized properties can this session actually read?
------------------------------------------------------------------------------
local function sweepInstance(instance: Instance)
	local rs = game:GetService("ReflectionService")
	local props = rs:GetPropertiesOfClass(instance.ClassName)
	if not props then
		return { error = "no reflection data for " .. instance.ClassName }
	end

	local readable, unreadable = {}, {}
	for _, p in props do
		if p.Serialized then
			local ok = pcall(function()
				return (instance :: any)[p.Name]
			end)
			table.insert(ok and readable or unreadable, p.Name)
		end
	end
	table.sort(readable)
	table.sort(unreadable)
	return {
		serializedTotal = #readable + #unreadable,
		readableCount = #readable,
		unreadable = unreadable, -- the exact set the projection cannot capture
	}
end

section("propertyAccess", function()
	results.propertyAccess = {
		Workspace = sweepInstance(workspace),
		Terrain = sweepInstance(workspace.Terrain),
		Lighting = sweepInstance(game:GetService("Lighting")),
		StarterPlayer = sweepInstance(game:GetService("StarterPlayer")),
		MaterialService = sweepInstance(game:GetService("MaterialService")),
	}
end)

------------------------------------------------------------------------------
-- 3. SerializationService behavior
------------------------------------------------------------------------------
section("serialization", function()
	local ss = game:GetService("SerializationService")
	local out = {}

	-- Creatable roundtrip + repeat-byte-stability
	local folder = Instance.new("Folder")
	folder.Name = "ProbeFolder"
	local part = Instance.new("Part")
	part.Name = "ProbePart"
	part.Parent = folder

	local b1 = ss:SerializeInstancesAsync({ folder })
	local b2 = ss:SerializeInstancesAsync({ folder })
	out.folderBlobBytes = buffer.len(b1)
	out.repeatSerializeIdentical = buffer.tostring(b1) == buffer.tostring(b2)

	-- Roundtrip fidelity
	local okRt, rtOrErr = try(function()
		local instances = ss:DeserializeInstancesAsync(b1)
		return #instances .. " root(s), first: " .. instances[1].ClassName .. "/" .. instances[1].Name
	end)
	out.roundtrip = rtOrErr

	-- Non-creatable rejection: service as list root
	local okSvc, svcErr = try(function()
		return ss:SerializeInstancesAsync({ game:GetService("Lighting") })
	end)
	out.serializeServiceResult = okSvc and "UNEXPECTEDLY SUCCEEDED" or svcErr

	-- Non-creatable rejection: non-creatable container that is NOT a service
	local sps = game:GetService("StarterPlayer"):FindFirstChildOfClass("StarterPlayerScripts")
	if sps then
		local okSps, spsErr = try(function()
			return ss:SerializeInstancesAsync({ sps })
		end)
		out.serializeStarterPlayerScriptsResult = okSps and "UNEXPECTEDLY SUCCEEDED" or spsErr
	end

	-- Real content: StarterGui children (creatable), sized
	local guiChildren = game:GetService("StarterGui"):GetChildren()
	if #guiChildren > 0 then
		local okGui, guiOrErr = try(function()
			local blob = ss:SerializeInstancesAsync(guiChildren)
			return buffer.len(blob)
		end)
		out.starterGuiChildren = { count = #guiChildren, result = guiOrErr }
	end

	results.serialization = out
end)

------------------------------------------------------------------------------
-- 4. Terrain capture via CopyRegion
------------------------------------------------------------------------------
section("terrain", function()
	local terrain = workspace.Terrain
	local out = {}

	local extents = terrain.MaxExtents
	out.maxExtents = tostring(extents.Min) .. " -> " .. tostring(extents.Max)

	local t0 = os.clock()
	local okCopy, regionOrErr = try(function()
		return terrain:CopyRegion(extents)
	end)
	out.copyRegionSeconds = math.floor((os.clock() - t0) * 1000) / 1000

	if not okCopy then
		out.copyRegionError = regionOrErr
	else
		local region = regionOrErr
		out.regionClass = region.ClassName
		out.regionSize = tostring(region.SizeInCells)

		local ss = game:GetService("SerializationService")
		local okSer, blobOrErr = try(function()
			return ss:SerializeInstancesAsync({ region })
		end)
		if okSer then
			out.serializedBytes = buffer.len(blobOrErr)
			-- byte stability for embedded terrain blobs
			local okSer2, blob2 = try(function()
				return ss:SerializeInstancesAsync({ region })
			end)
			out.repeatSerializeIdentical = okSer2 and buffer.tostring(blobOrErr) == buffer.tostring(blob2) or nil
		else
			out.serializeError = blobOrErr
		end
	end

	results.terrain = out
end)

------------------------------------------------------------------------------
-- 5. Trial text projection (deterministic dump), small services only
------------------------------------------------------------------------------
section("projection", function()
	local rs = game:GetService("ReflectionService")
	local propCache = {}

	local function serializedProps(className: string)
		if propCache[className] then
			return propCache[className]
		end
		local names = {}
		local props = rs:GetPropertiesOfClass(className)
		if props then
			for _, p in props do
				if p.Serialized and p.Name ~= "Name" and p.Name ~= "Parent" then
					table.insert(names, p.Name)
				end
			end
		end
		table.sort(names)
		propCache[className] = names
		return names
	end

	local function encodeValue(v: any): string
		local t = typeof(v)
		if t == "Instance" then
			return "ref:" .. (v :: Instance):GetFullName()
		elseif t == "buffer" then
			return "buffer:" .. buffer.len(v) .. "B"
		end
		return t .. ":" .. tostring(v)
	end

	local function dump(inst: Instance, path: string, lines: { string })
		table.insert(lines, path .. " [" .. inst.ClassName .. "]")
		for _, propName in serializedProps(inst.ClassName) do
			local ok, value = pcall(function()
				return (inst :: any)[propName]
			end)
			if ok then
				table.insert(lines, "  " .. propName .. " = " .. encodeValue(value))
			end
		end
		for _, tag in inst:GetTags() do
			table.insert(lines, "  @tag " .. tostring(tag))
		end
		for name, value in inst:GetAttributes() do
			table.insert(lines, "  @attr " .. name .. " = " .. encodeValue(value))
		end
		local children = inst:GetChildren()
		table.sort(children, function(a, b)
			return a.Name < b.Name
		end)
		for _, child in children do
			dump(child, path .. "/" .. child.Name, lines)
		end
	end

	local out = {}
	for _, serviceName in { "Lighting", "MaterialService", "TextChatService" } do
		local lines = {}
		dump(game:GetService(serviceName), serviceName, lines)
		local full = table.concat(lines, "\n")
		out[serviceName] = {
			lineCount = #lines,
			byteCount = #full,
			sample = string.sub(full, 1, 2000),
		}
	end
	results.projection = out
end)

return results
