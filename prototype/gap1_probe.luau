--[[
	gap1_probe.luau — investigates the hidden-state gap (instances whose essence
	lives in serialized-but-unreadable properties, e.g. MeshPart, UnionOperation).

	1. Baseline: which unreadable serialized props exist on EVERY instance
	   (Instance-level internals) — these are ignorable noise, not hidden state.
	2. Census: every class present in this place, its instance count, and its
	   unreadable serialized props beyond the baseline → the real must-blob list.
	3. MeshPart deep test (first one found): blob roundtrip stability, and
	   whether text-style reconstruction is even possible (MeshContent writable?
	   AssetService:CreateMeshPartAsync available?).
	4. UnionOperation deep test if any exist.
]]

local SerializationService = game:GetService("SerializationService")
local ReflectionService = game:GetService("ReflectionService")

local results = { meta = { placeVersion = game.PlaceVersion } }

local function section(name: string, fn: () -> ())
	local ok, err = pcall(fn)
	if not ok then
		results[name] = { sectionError = tostring(err) }
	end
end

-- Unreadable serialized props for a class, probed against a representative instance
local sweepCache = {}
local function unreadableSerialized(inst: Instance): { string }
	local className = inst.ClassName
	if sweepCache[className] then
		return sweepCache[className]
	end
	local out = {}
	local props = ReflectionService:GetPropertiesOfClass(className)
	if props then
		for _, p in props do
			if p.Serialized then
				local ok = pcall(function()
					return (inst :: any)[p.Name]
				end)
				if not ok then
					table.insert(out, p.Name)
				end
			end
		end
	end
	table.sort(out)
	sweepCache[className] = out
	return out
end

local baselineSet = {}

------------------------------------------------------------------------------
-- 1. Baseline internals from a fresh Folder (pure Instance-level noise)
------------------------------------------------------------------------------
section("baseline", function()
	local folder = Instance.new("Folder")
	local list = unreadableSerialized(folder)
	for _, name in list do
		baselineSet[name] = true
	end
	results.baseline = list
end)

------------------------------------------------------------------------------
-- 2. Whole-place class census with flagged hidden-state props
------------------------------------------------------------------------------
section("census", function()
	local counts = {}
	local representative = {}
	local total = 0
	for _, inst in game:GetDescendants() do
		total += 1
		local c = inst.ClassName
		counts[c] = (counts[c] or 0) + 1
		if not representative[c] then
			representative[c] = inst
		end
	end

	local flaggedClasses = {}
	local cleanClasses = {}
	for className, inst in representative do
		local okSweep, unreadable = pcall(unreadableSerialized, inst)
		if not okSweep then
			flaggedClasses[className] = { count = counts[className], sweepError = tostring(unreadable) }
			continue
		end
		local flagged = {}
		for _, name in unreadable do
			if not baselineSet[name] then
				table.insert(flagged, name)
			end
		end
		if #flagged > 0 then
			flaggedClasses[className] = { count = counts[className], hidden = flagged }
		else
			table.insert(cleanClasses, className)
		end
	end
	table.sort(cleanClasses)
	results.census = {
		totalInstances = total,
		flaggedClasses = flaggedClasses,
		cleanClassCount = #cleanClasses,
		cleanClasses = cleanClasses,
	}
end)

------------------------------------------------------------------------------
-- 3. MeshPart deep test
------------------------------------------------------------------------------
section("meshPart", function()
	local target: MeshPart? = nil
	for _, inst in game:GetDescendants() do
		if inst:IsA("MeshPart") then
			target = inst
			break
		end
	end
	if not target then
		results.meshPart = { found = false }
		return
	end
	local mp = target :: MeshPart
	local out: { [string]: any } = { found = true, path = mp:GetFullName() }

	-- readable geometry-adjacent props
	for _, propName in { "MeshId", "MeshContent", "CollisionFidelity", "RenderFidelity", "FluidFidelity" } do
		local ok, v = pcall(function()
			return tostring((mp :: any)[propName])
		end)
		out["read_" .. propName] = ok and v or "UNREADABLE"
	end

	-- blob roundtrip
	local b1 = SerializationService:SerializeInstancesAsync({ mp })
	local list = SerializationService:DeserializeInstancesAsync(b1)
	local b2 = SerializationService:SerializeInstancesAsync(list)
	out.blobBytes = buffer.len(b1)
	out.roundtripBytes = buffer.len(b2)
	out.roundtripIdentical = buffer.tostring(b1) == buffer.tostring(b2)

	-- text-style reconstruction attempts
	local okNew, freshOrErr = pcall(function()
		return Instance.new("MeshPart")
	end)
	out.instanceNew = okNew and "OK" or tostring(freshOrErr)
	if okNew then
		local fresh = freshOrErr :: MeshPart
		local okSetContent, errSetContent = pcall(function()
			(fresh :: any).MeshContent = (mp :: any).MeshContent
		end)
		out.setMeshContent = okSetContent and "OK" or tostring(errSetContent)
		local okSetId, errSetId = pcall(function()
			(fresh :: any).MeshId = (mp :: any).MeshId
		end)
		out.setMeshId = okSetId and "OK" or tostring(errSetId)
		-- how much smaller is a text-reconstructed mesh part? (hidden-state loss indicator)
		local okBlob, freshBlob = pcall(function()
			return SerializationService:SerializeInstancesAsync({ fresh })
		end)
		out.freshBlobBytes = okBlob and buffer.len(freshBlob) or tostring(freshBlob)
	end

	-- sanctioned reconstruction path
	local okCreate, createdOrErr = pcall(function()
		local AssetService = game:GetService("AssetService")
		return AssetService:CreateMeshPartAsync((mp :: any).MeshContent)
	end)
	if okCreate then
		local okBlob, blob = pcall(function()
			return SerializationService:SerializeInstancesAsync({ createdOrErr })
		end)
		out.createMeshPartAsync = "OK, blob=" .. (okBlob and tostring(buffer.len(blob)) or tostring(blob))
	else
		out.createMeshPartAsync = tostring(createdOrErr)
	end

	results.meshPart = out
end)

------------------------------------------------------------------------------
-- 4. UnionOperation deep test (if any exist)
------------------------------------------------------------------------------
section("union", function()
	local target: UnionOperation? = nil
	for _, inst in game:GetDescendants() do
		if inst.ClassName == "UnionOperation" then
			target = inst
			break
		end
	end
	if not target then
		results.union = { found = false }
		return
	end
	local u = target :: UnionOperation
	local b1 = SerializationService:SerializeInstancesAsync({ u })
	local list = SerializationService:DeserializeInstancesAsync(b1)
	local b2 = SerializationService:SerializeInstancesAsync(list)
	local fresh = Instance.new("UnionOperation")
	local freshBlob = SerializationService:SerializeInstancesAsync({ fresh })
	results.union = {
		found = true,
		path = u:GetFullName(),
		blobBytes = buffer.len(b1),
		roundtripIdentical = buffer.tostring(b1) == buffer.tostring(b2),
		freshBlobBytes = buffer.len(freshBlob),
	}
end)

return results
