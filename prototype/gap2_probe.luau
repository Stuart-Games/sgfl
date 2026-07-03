--[[
	gap2_probe.luau — investigates the instance-identity gap for refs and diffs.

	1. Is Instance.UniqueId readable in an execution session?
	2. Does UniqueId survive SerializeInstancesAsync -> DeserializeInstancesAsync?
	   (If regenerated on deserialize, uid-based identity churns on every apply.)
	3. Duplicate-sibling-name census: how often would path-based identity need
	   disambiguation in this place?
	4. Ref-property census: every Instance-typed serialized property value in the
	   place — how many refs exist, and how many cross top-level container
	   boundaries (approximating assets.json entry boundaries)?
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

------------------------------------------------------------------------------
-- 1 + 2. UniqueId readability and serialization stability
------------------------------------------------------------------------------
section("uniqueId", function()
	local out = {}

	local part = Instance.new("Part")
	part.Name = "UidProbe"
	local okRead, uidOrErr = pcall(function()
		return tostring((part :: any).UniqueId)
	end)
	out.readableOnFresh = okRead and ("OK: " .. uidOrErr) or tostring(uidOrErr)

	local okReadWs, wsUid = pcall(function()
		return tostring((workspace.Terrain :: any).UniqueId)
	end)
	out.readableOnExisting = okReadWs and ("OK: " .. wsUid) or tostring(wsUid)

	if okRead then
		local blob = SerializationService:SerializeInstancesAsync({ part })
		local list = SerializationService:DeserializeInstancesAsync(blob)
		local okRead2, uid2 = pcall(function()
			return tostring((list[1] :: any).UniqueId)
		end)
		if okRead2 then
			out.survivesRoundtrip = (uid2 == uidOrErr)
			out.roundtripUid = uid2
		else
			out.survivesRoundtrip = "unreadable after roundtrip: " .. tostring(uid2)
		end
		-- second deserialize of the SAME blob: same uid again, or fresh each time?
		local list2 = SerializationService:DeserializeInstancesAsync(blob)
		local okRead3, uid3 = pcall(function()
			return tostring((list2[1] :: any).UniqueId)
		end)
		if okRead3 then
			out.secondDeserializeSameUid = (uid3 == tostring((list[1] :: any).UniqueId))
		end
	end

	results.uniqueId = out
end)

------------------------------------------------------------------------------
-- 3. Duplicate sibling name census
------------------------------------------------------------------------------
section("duplicateNames", function()
	local totalInstances = 0
	local parentsWithDuplicates = 0
	local duplicateGroups = 0
	local duplicatedInstances = 0
	local examples = {}

	local function visit(parent: Instance)
		local names = {}
		for _, child in parent:GetChildren() do
			names[child.Name] = (names[child.Name] or 0) + 1
			totalInstances += 1
			visit(child)
		end
		local hasDup = false
		for name, n in names do
			if n > 1 then
				hasDup = true
				duplicateGroups += 1
				duplicatedInstances += n
				if #examples < 15 then
					table.insert(examples, parent:GetFullName() .. " -> " .. name .. " x" .. n)
				end
			end
		end
		if hasDup then
			parentsWithDuplicates += 1
		end
	end
	visit(game)

	results.duplicateNames = {
		totalInstances = totalInstances,
		parentsWithDuplicates = parentsWithDuplicates,
		duplicateGroups = duplicateGroups,
		duplicatedInstances = duplicatedInstances,
		examples = examples,
	}
end)

------------------------------------------------------------------------------
-- 4. Ref-property census
------------------------------------------------------------------------------
section("refs", function()
	-- per-class list of readable serialized props (cached)
	local propCache = {}
	local function readableProps(inst: Instance): { string }
		local className = inst.ClassName
		if propCache[className] then
			return propCache[className]
		end
		local names = {}
		local props = ReflectionService:GetPropertiesOfClass(className)
		if props then
			for _, p in props do
				if p.Serialized and p.Name ~= "Parent" then
					local ok = pcall(function()
						return (inst :: any)[p.Name]
					end)
					if ok then
						table.insert(names, p.Name)
					end
				end
			end
		end
		propCache[className] = names
		return names
	end

	-- approximate an assets.json entry key: service name, plus second level for
	-- container services whose entries are per-folder
	local perFolderServices = { ReplicatedStorage = true, ServerStorage = true }
	local function entryKey(inst: Instance): string
		local chain = {}
		local cursor: Instance? = inst
		while cursor and cursor ~= game do
			table.insert(chain, 1, cursor)
			cursor = cursor.Parent
		end
		if #chain == 0 then
			return "game"
		end
		local serviceName = chain[1].ClassName
		if perFolderServices[serviceName] and chain[2] then
			return serviceName .. "." .. chain[2].Name
		end
		return serviceName
	end

	local totalRefs = 0
	local nilRefs = 0
	local sameEntry = 0
	local crossEntry = 0
	local crossExamples = {}
	local refPropCounts = {}

	for _, inst in game:GetDescendants() do
		for _, propName in readableProps(inst) do
			local ok, value = pcall(function()
				return (inst :: any)[propName]
			end)
			if ok and typeof(value) == "Instance" then
				totalRefs += 1
				refPropCounts[inst.ClassName .. "." .. propName] = (refPropCounts[inst.ClassName .. "." .. propName] or 0) + 1
				local holderKey = entryKey(inst)
				local targetKey = entryKey(value)
				if holderKey == targetKey then
					sameEntry += 1
				else
					crossEntry += 1
					if #crossExamples < 15 then
						table.insert(
							crossExamples,
							inst:GetFullName() .. "." .. propName .. " -> " .. value:GetFullName()
						)
					end
				end
			elseif ok and value == nil then
				-- can't distinguish nil-ref from nil-other cheaply; skip
				nilRefs += 1
			end
		end
	end

	results.refs = {
		totalRefs = totalRefs,
		sameEntry = sameEntry,
		crossEntry = crossEntry,
		refPropCounts = refPropCounts,
		crossExamples = crossExamples,
	}
end)

return results
