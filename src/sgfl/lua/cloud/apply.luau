--[[
	apply_v2.luau — the publish-direction interpreter.

	Runs version-pinned to a freshly uploaded rojo-built Saved version. Reads
	the project's entry files (shipped via BinaryInput in the same
	manifest+data container format the projection emits), rebuilds each
	assets.json entry inside the live DataModel, runs the optional post-apply
	hook, then AssetService:SavePlaceAsync().

	Two global passes: pass 1 creates instances and sets scalar properties for
	every entry; pass 2 resolves Instance refs (entry-relative and absolute)
	after everything exists.

	The driver fills the CONFIG placeholder below with the same config JSON
	as the projection, and the POSTAPPLY placeholder with `nil` or the
	inlined postapply.luau hook. The placeholder tokens must appear exactly
	once in this file — never write them in comments (the driver's
	replacement is global, and loadCloudScript enforces the count).

	[!sidecar ...] blocks are ignored here — the driver patches those values
	into the saved binary afterwards.

	mode = "children": entry files live under the entry's dedicated folder as
	{Entry}.sgfl (container root block only) plus one {ChildName}.sgfl /
	{ChildName}.sgfl.rbxm per managed child (discovered by folder prefix in
	the input container's manifest, since child names aren't in CONFIG).
	Filters (include/exclude) additionally gate what clearEntryRoot destroys,
	on both "file" and "children" mode entries.
]]

local HttpService = game:GetService("HttpService")
local SerializationService = game:GetService("SerializationService")
local AssetService = game:GetService("AssetService")

local CONFIG = HttpService:JSONDecode([===[__SGFL_CONFIG__]===])
local POSTAPPLY = __SGFL_POSTAPPLY__

local warnings = {}
local function addWarning(message: string)
	table.insert(warnings, message)
	print("WARN: " .. message)
end

------------------------------------------------------------------------------
-- Input container
------------------------------------------------------------------------------

local taskInput = ({ ... })[1]
assert(taskInput and taskInput.BinaryInput, "apply_v2 requires a BinaryInput container")
local inputBuffer: buffer = taskInput.BinaryInput
local manifestLen = buffer.readu32(inputBuffer, 0)
local inputManifest = HttpService:JSONDecode(buffer.readstring(inputBuffer, 4, manifestLen))
local inputFiles: { [string]: string } = {}
for _, fileEntry in inputManifest.files do
	inputFiles[fileEntry.name] = buffer.readstring(inputBuffer, 4 + manifestLen + fileEntry.offset, fileEntry.size)
end

------------------------------------------------------------------------------
-- Base64 decode (islands)
------------------------------------------------------------------------------

local BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local BASE64_LOOKUP: { [string]: number } = {}
for i = 1, #BASE64_ALPHABET do
	BASE64_LOOKUP[BASE64_ALPHABET:sub(i, i)] = i - 1
end

local function base64Decode(s: string): string
	s = s:gsub("=", "")
	local out = table.create(math.floor(#s * 3 / 4))
	for i = 1, #s, 4 do
		local a = BASE64_LOOKUP[s:sub(i, i)] or 0
		local b = BASE64_LOOKUP[s:sub(i + 1, i + 1)] or 0
		local c = BASE64_LOOKUP[s:sub(i + 2, i + 2)]
		local d = BASE64_LOOKUP[s:sub(i + 3, i + 3)]
		local n = a * 262144 + b * 4096 + (c or 0) * 64 + (d or 0)
		table.insert(out, string.char(bit32.extract(n, 16, 8)))
		if c then
			table.insert(out, string.char(bit32.extract(n, 8, 8)))
		end
		if d then
			table.insert(out, string.char(bit32.extract(n, 0, 8)))
		end
	end
	return table.concat(out)
end

------------------------------------------------------------------------------
-- Text format parser
------------------------------------------------------------------------------

local function unescapeSegment(segment: string): (string, number)
	-- returns (name, occurrenceIndex); ":N" suffix only matches unescaped ":"
	local name = segment
	local occurrence = 1
	local base, suffix = segment:match("^(.-[^\\]):(%d+)$")
	if base then
		name = base
		occurrence = tonumber(suffix) :: number
	end
	name = name:gsub("\\(.)", function(c)
		if c == "n" then
			return "\n"
		elseif c == "r" then
			return "\r"
		elseif c == "t" then
			return "\t"
		end
		return c
	end)
	return name, occurrence
end

-- split on unescaped "/" only, keeping the raw (still-escaped) segment strings
local function splitRawSegments(pathText: string): { string }
	local raws = {}
	local current = {}
	local i = 1
	while i <= #pathText do
		local c = pathText:sub(i, i)
		if c == "\\" then
			table.insert(current, pathText:sub(i, i + 1))
			i += 2
		elseif c == "/" then
			table.insert(raws, table.concat(current))
			current = {}
			i += 1
		else
			table.insert(current, c)
			i += 1
		end
	end
	table.insert(raws, table.concat(current))
	return raws
end

local function splitPath(pathText: string): { { name: string, occurrence: number } }
	if pathText == "." then
		return {}
	end
	local segments = {}
	for _, raw in splitRawSegments(pathText) do
		local name, occurrence = unescapeSegment(raw)
		table.insert(segments, { name = name, occurrence = occurrence })
	end
	return segments
end

type Block = {
	pathText: string,
	className: string,
	isBlob: boolean,
	isSidecar: boolean,
	dataB64: string?,
	props: { [string]: string }, -- raw value text
	heredocs: { [string]: string },
	attrs: { [string]: string },
	tags: string?,
	styles: { [string]: string },
}

local function parseEntryText(text: string, entryLabel: string): { Block }
	local blocks: { Block } = {}
	local current: Block? = nil
	local lines = text:split("\n")
	local i = 1
	while i <= #lines do
		local line = lines[i]
		-- structural lines never end in a raw \r (heredoc bodies, which may,
		-- are consumed below and never reach this check): CRLF conversion
		-- by a git checkout must fail loudly, not clear-and-skip subtrees
		if line:sub(-1) == "\r" then
			error(
				entryLabel
					.. ": entry file has CRLF line endings (git core.autocrlf likely converted it) — "
					.. "renormalize to LF and add a .gitattributes rule: *.sgfl text eol=lf"
			)
		end
		if line == "" then
			i += 1
			continue
		end
		local sidecarHeader = line:match("^%[!sidecar (.*)%]$")
		if sidecarHeader then
			current = nil -- driver-owned; ignore following prop lines
			i += 1
			continue
		end
		local headerPath, headerClass, blobFlag = line:match("^%[(.*)%] ([%w_]+)( !blob)$")
		if not headerPath then
			headerPath, headerClass = line:match("^%[(.*)%] ([%w_]+)$")
		end
		if headerPath then
			current = {
				pathText = headerPath,
				className = headerClass :: string,
				isBlob = blobFlag ~= nil,
				isSidecar = false,
				dataB64 = nil,
				props = {},
				heredocs = {},
				attrs = {},
				tags = nil,
				styles = {},
			}
			table.insert(blocks, current)
			i += 1
			continue
		end
		local name, valueText = line:match("^([^%s=]+) = (.*)$")
		if not name then
			addWarning("unparseable line: " .. line:sub(1, 120))
			i += 1
			continue
		end
		if valueText == "<<<" then
			-- heredoc: tab-prefixed raw lines until a bare >>> line
			local raw = {}
			i += 1
			while i <= #lines and lines[i] ~= ">>>" do
				table.insert(raw, (lines[i]:gsub("^\t", "")))
				i += 1
			end
			valueText = nil
			if current then
				current.heredocs[name] = table.concat(raw, "\n")
			end
			i += 1 -- consume >>>
		else
			i += 1
		end
		if not current then
			continue -- sidecar or stray content
		end
		if valueText == nil then
			continue -- heredoc already stored
		elseif name == "!data" then
			current.dataB64 = valueText
		elseif name == "!hidden" then
			-- informational
		elseif name == "@Tags" then
			current.tags = valueText
		elseif name:sub(1, 6) == "@Attr." then
			current.attrs[name:sub(7)] = valueText
		elseif name:sub(1, 7) == "@Style." then
			current.styles[name:sub(8)] = valueText
		else
			current.props[name] = valueText
		end
	end
	return blocks
end

------------------------------------------------------------------------------
-- Value decoding
------------------------------------------------------------------------------

local function unescapeString(s: string): string
	return (s:gsub("\\x(%x%x)", function(hex)
		return string.char(tonumber(hex, 16) :: number)
	end):gsub("\\(.)", function(c)
		if c == "n" then
			return "\n"
		elseif c == "r" then
			return "\r"
		elseif c == "t" then
			return "\t"
		end
		return c
	end))
end

local function parseNumber(s: string): number?
	if s == "nan" then
		return 0 / 0
	elseif s == "inf" then
		return math.huge
	elseif s == "-inf" then
		return -math.huge
	end
	return tonumber(s)
end

-- split "a, b, c" respecting quoted strings
local function splitArgs(s: string): { string }
	local args = {}
	local current = {}
	local inString = false
	local i = 1
	while i <= #s do
		local c = s:sub(i, i)
		if inString then
			table.insert(current, c)
			if c == "\\" then
				table.insert(current, s:sub(i + 1, i + 1))
				i += 1
			elseif c == '"' then
				inString = false
			end
		elseif c == '"' then
			inString = true
			table.insert(current, c)
		elseif c == "," then
			table.insert(args, (table.concat(current):gsub("^%s+", "")))
			current = {}
		else
			table.insert(current, c)
		end
		i += 1
	end
	local last = table.concat(current):gsub("^%s+", "")
	if #last > 0 then
		table.insert(args, last)
	end
	return args
end

local SKIP = newproxy(false) -- sentinel: property line understood but not applied

local decodeValue -- forward

local function numericArgs(argText: string): { number }
	local out = {}
	for _, arg in splitArgs(argText) do
		table.insert(out, parseNumber(arg) :: number)
	end
	return out
end

local CONSTRUCTORS: { [string]: (string) -> any } = {
	Color3 = function(argText)
		local a = numericArgs(argText)
		return Color3.new(a[1], a[2], a[3])
	end,
	Vector2 = function(argText)
		local a = numericArgs(argText)
		return Vector2.new(a[1], a[2])
	end,
	Vector3 = function(argText)
		local a = numericArgs(argText)
		return Vector3.new(a[1], a[2], a[3])
	end,
	Vector2int16 = function(argText)
		local a = numericArgs(argText)
		return Vector2int16.new(a[1], a[2])
	end,
	Vector3int16 = function(argText)
		local a = numericArgs(argText)
		return Vector3int16.new(a[1], a[2], a[3])
	end,
	CFrame = function(argText)
		return CFrame.new(table.unpack(numericArgs(argText)))
	end,
	UDim = function(argText)
		local a = numericArgs(argText)
		return UDim.new(a[1], a[2])
	end,
	UDim2 = function(argText)
		local a = numericArgs(argText)
		return UDim2.new(a[1], a[2], a[3], a[4])
	end,
	Rect = function(argText)
		local a = numericArgs(argText)
		return Rect.new(a[1], a[2], a[3], a[4])
	end,
	NumberRange = function(argText)
		local a = numericArgs(argText)
		return NumberRange.new(a[1], a[2])
	end,
	BrickColor = function(argText)
		return BrickColor.new(tonumber(argText) :: number)
	end,
	PhysicalProperties = function(argText)
		return PhysicalProperties.new(table.unpack(numericArgs(argText)))
	end,
	Font = function(argText)
		local args = splitArgs(argText)
		local family = unescapeString(args[1]:sub(2, -2))
		return Font.new(family, (Enum.FontWeight :: any)[args[2]], (Enum.FontStyle :: any)[args[3]])
	end,
	Content = function(argText)
		if argText == "" then
			return Content.none
		end
		return Content.fromUri(unescapeString(argText:sub(2, -2)))
	end,
	Faces = function(argText)
		local ids = {}
		for _, name in splitArgs(argText) do
			table.insert(ids, (Enum.NormalId :: any)[name])
		end
		return Faces.new(table.unpack(ids))
	end,
	Axes = function(argText)
		local ids = {}
		for _, name in splitArgs(argText) do
			table.insert(ids, (Enum.Axis :: any)[name])
		end
		return Axes.new(table.unpack(ids))
	end,
	SecurityCapabilities = function()
		return SKIP -- only the empty default is ever emitted; leave engine default
	end,
	Ref = function()
		return SKIP -- refs are resolved in pass 2
	end,
}

local function parseKeypointList(argText: string): { { number } }
	local keypoints = {}
	for group in argText:gmatch("%(([^%)]*)%)") do
		table.insert(keypoints, numericArgs(group))
	end
	return keypoints
end

decodeValue = function(valueText: string): any
	if valueText == "true" then
		return true
	elseif valueText == "false" then
		return false
	end
	local n = parseNumber(valueText)
	if n ~= nil then
		return n
	end
	if valueText:sub(1, 1) == '"' then
		return unescapeString(valueText:sub(2, -2))
	end
	local enumType, enumName = valueText:match("^Enum%.([%w_]+)%.([%w_ ]+)$")
	if enumType then
		return (Enum :: any)[enumType][enumName]
	end
	local seqName, seqArgs = valueText:match("^(%w+)%[(.*)%]$")
	if seqName == "NumberSequence" then
		local keypoints = {}
		for _, kp in parseKeypointList(seqArgs) do
			table.insert(keypoints, NumberSequenceKeypoint.new(kp[1], kp[2], kp[3]))
		end
		return NumberSequence.new(keypoints)
	elseif seqName == "ColorSequence" then
		local keypoints = {}
		for _, kp in parseKeypointList(seqArgs) do
			table.insert(keypoints, ColorSequenceKeypoint.new(kp[1], Color3.new(kp[2], kp[3], kp[4])))
		end
		return ColorSequence.new(keypoints)
	end
	local ctorName, ctorArgs = valueText:match("^(%w+)%((.*)%)$")
	if ctorName then
		local constructor = CONSTRUCTORS[ctorName]
		if constructor then
			return constructor(ctorArgs)
		end
	end
	error("cannot decode value: " .. valueText:sub(1, 120))
end

------------------------------------------------------------------------------
-- Apply
------------------------------------------------------------------------------

local rojoExcluded = {}
for _, p in (CONFIG.rojoPaths or {}) :: { string } do
	rojoExcluded[p] = true
end

------------------------------------------------------------------------------
-- Entry filters: include/exclude name globs ("Temp*") or "$ClassName" (IsA).
-- Kept identical to projection.luau's copy — the two must stay symmetric.
------------------------------------------------------------------------------

local MAGIC_CHARS = "().%+-*?[]^$"
local function globToPattern(glob: string): string
	local out = { "^" }
	for i = 1, #glob do
		local c = glob:sub(i, i)
		if c == "*" then
			table.insert(out, ".-")
		elseif MAGIC_CHARS:find(c, 1, true) then
			table.insert(out, "%" .. c)
		else
			table.insert(out, c)
		end
	end
	table.insert(out, "$")
	return table.concat(out)
end

local function matchesFilterPattern(inst: Instance, pattern: string): boolean
	if pattern:sub(1, 1) == "$" then
		return inst:IsA(pattern:sub(2))
	end
	return inst.Name:match(globToPattern(pattern)) ~= nil
end

-- include-then-exclude precedence; no include list = everything included
local function isManaged(inst: Instance, entry): boolean
	local include = entry.include :: { string }
	local exclude = entry.exclude :: { string }
	if include and #include > 0 then
		local included = false
		for _, pattern in include do
			if matchesFilterPattern(inst, pattern) then
				included = true
				break
			end
		end
		if not included then
			return false
		end
	end
	if exclude then
		for _, pattern in exclude do
			if matchesFilterPattern(inst, pattern) then
				return false
			end
		end
	end
	return true
end

local function findOccurrence(parent: Instance, name: string, occurrence: number): Instance?
	local seen = 0
	for _, child in parent:GetChildren() do
		if child.Name == name then
			seen += 1
			if seen == occurrence then
				return child
			end
		end
	end
	return nil
end

-- pending ref assignments for pass 2: {holder, propName, refText, entryRoot}
local pendingRefs: { { holder: Instance, propName: string, refText: string, entryRoot: Instance } } = {}

-- props the session cannot write (capability asymmetry): returned to the
-- driver, which patches them into the saved binary instead. Only singleton
-- entry roots (services) qualify — the binary patcher targets whole classes.
local patchProps: { { className: string, prop: string, value: any } } = {}

local function routeToPatch(inst: Instance, entryRoot: Instance, name: string, value: any): boolean
	if inst ~= entryRoot or inst.Parent ~= game then
		return false
	end
	local t = typeof(value)
	if t == "EnumItem" then
		table.insert(patchProps, { className = inst.ClassName, prop = name, value = (value :: EnumItem).Value })
		return true
	end
	if t == "boolean" or t == "number" or t == "string" then
		table.insert(patchProps, { className = inst.ClassName, prop = name, value = value })
		return true
	end
	return false
end

local function applyProps(inst: Instance, block: Block, entryRoot: Instance, pathLabel: string)
	local propNames = {}
	for name in block.props do
		table.insert(propNames, name)
	end
	table.sort(propNames)
	for _, name in propNames do
		local valueText = block.props[name]
		if valueText:sub(1, 4) == "Ref(" then
			table.insert(
				pendingRefs,
				{ holder = inst, propName = name, refText = valueText:sub(5, -2), entryRoot = entryRoot }
			)
			continue
		end
		local okDecode, value = pcall(decodeValue, valueText)
		if not okDecode then
			addWarning(pathLabel .. "." .. name .. ": " .. tostring(value))
			continue
		end
		if value == SKIP then
			continue
		end
		local okSet, setErr = pcall(function()
			(inst :: any)[name] = value
		end)
		if not okSet and name:sub(1, 1):match("%l") then
			-- Files saved before the canonical-name fix carry deprecated
			-- lowercase serialized aliases (size, shape, ...) whose setters
			-- are gone; retry with the capitalized modern name
			okSet = pcall(function()
				(inst :: any)[name:sub(1, 1):upper() .. name:sub(2)] = value
			end)
		end
		if not okSet then
			if not routeToPatch(inst, entryRoot, name, value) then
				addWarning(pathLabel .. "." .. name .. " set failed: " .. tostring(setErr))
			end
		end
	end
	for name, raw in block.heredocs do
		local okSet, setErr = pcall(function()
			(inst :: any)[name] = raw
		end)
		if not okSet then
			addWarning(pathLabel .. "." .. name .. " (heredoc) set failed: " .. tostring(setErr))
		end
	end
	for name, valueText in block.attrs do
		if name:sub(1, 4) == "RBX_" then
			continue -- Roblox-internal, CoreScript-gated; engine re-derives them
		end
		local okDecode, value = pcall(decodeValue, valueText)
		if okDecode and value ~= SKIP then
			local okSet, setErr = pcall(function()
				inst:SetAttribute(name, value)
			end)
			if not okSet then
				addWarning(pathLabel .. " attribute " .. name .. ": " .. tostring(setErr))
			end
		else
			addWarning(pathLabel .. " attribute " .. name .. ": " .. tostring(value))
		end
	end
	if block.tags then
		for tag in block.tags:gmatch('"([^"]*)"') do
			inst:AddTag(unescapeString(tag))
		end
	end
	local styleNames = {}
	for name in block.styles do
		table.insert(styleNames, name)
	end
	if #styleNames > 0 then
		local styleValues = {}
		for _, name in styleNames do
			local okDecode, value = pcall(decodeValue, block.styles[name])
			if okDecode then
				styleValues[name] = value
			else
				addWarning(pathLabel .. " style " .. name .. ": " .. tostring(value))
			end
		end
		local okSet, setErr = pcall(function()
			(inst :: any):SetProperties(styleValues)
		end)
		if not okSet then
			addWarning(pathLabel .. " SetProperties failed: " .. tostring(setErr))
		end
	end
end

-- merge a deserialized Terrain into the live singleton
local function mergeTerrain(deserialized: Instance)
	local live = workspace.Terrain :: any
	local incoming = deserialized :: any
	local okRegion, regionErr = pcall(function()
		local region = incoming:CopyRegion(incoming.MaxExtents)
		live:Clear()
		live:PasteRegion(region, live.MaxExtents.Min, true)
	end)
	if not okRegion then
		addWarning("Terrain voxel merge failed: " .. tostring(regionErr))
	end
	for _, material in Enum.Material:GetEnumItems() do
		pcall(function()
			live:SetMaterialColor(material, incoming:GetMaterialColor(material))
		end)
	end
	for _, propName in { "WaterColor", "WaterReflectance", "WaterTransparency", "WaterWaveSize", "WaterWaveSpeed" } do
		pcall(function()
			live[propName] = incoming[propName]
		end)
	end
end

local function reparentBlobChildren(list: { Instance }, target: Instance)
	for _, inst in list do
		if inst.ClassName == "Terrain" and target == workspace then
			mergeTerrain(inst)
		else
			inst.Parent = target
		end
	end
end

local function resolveEntryRoot(robloxPath: string): (Instance?, string?)
	local segments = robloxPath:split(".")
	local service = game:FindFirstChild(segments[1])
	if not service then
		local ok, fetched = pcall(function()
			return game:GetService(segments[1] :: any)
		end)
		service = if ok then fetched else nil
	end
	if not service then
		return nil, "service " .. segments[1] .. " not found"
	end
	local cursor = service
	for i = 2, #segments do
		local nextChild = cursor:FindFirstChild(segments[i])
		if not nextChild then
			nextChild = Instance.new("Folder")
			nextChild.Name = segments[i]
			nextChild.Parent = cursor
		end
		cursor = nextChild
	end
	return cursor, nil
end

local function clearEntryRoot(root: Instance, rootAbsolute: { string }, isService: boolean, entry)
	for _, child in root:GetChildren() do
		if not child.Archivable then
			continue
		end
		if child:IsA("Camera") or child.ClassName == "Terrain" then
			continue
		end
		if isService then
			local childPath = table.concat(rootAbsolute, "/") .. "/" .. child.Name
			if rojoExcluded[childPath] then
				continue
			end
		end
		if not isManaged(child, entry) then
			continue
		end
		if isService then
			-- non-creatable singletons (StarterPlayerScripts etc.) are reused, not destroyed
			local okNew, probe = pcall(function()
				return Instance.new(child.ClassName)
			end)
			if not okNew then
				continue
			end
			(probe :: Instance):Destroy()
		end
		child:Destroy()
	end
end

local function pathOfSegments(inst: Instance): { string }
	local segments = {}
	local cursor: Instance? = inst
	while cursor and cursor ~= game do
		table.insert(segments, 1, cursor.Name)
		cursor = cursor.Parent
	end
	return segments
end

-- Build the instances described by `blocks` beneath `root` (root block first,
-- path "."), resolving parents by path as they're created. `entryRoot` is the
-- base "./..." refs in this block set resolve against (== root, except when
-- applying a children-mode child's own file, where the child is its own
-- ref-relative root). `labelPrefix` is used in warning messages.
local function applyBlocks(blocks: { Block }, root: Instance, entryRoot: Instance, labelPrefix: string)
	local instanceByPath: { [string]: Instance } = { ["."] = root }
	for _, block in blocks do
		if block.isSidecar then
			continue
		end
		if block.pathText == "." then
			applyProps(root, block, entryRoot, labelPrefix .. ":.")
			continue
		end
		local segments = splitPath(block.pathText)
		local rawSegments = splitRawSegments(block.pathText)
		local parentPath = "."
		if #rawSegments > 1 then
			parentPath = table.concat(rawSegments, "/", 1, #rawSegments - 1)
		end
		local parent = instanceByPath[parentPath]
		if not parent then
			addWarning(labelPrefix .. ": parent not found for " .. block.pathText)
			continue
		end
		local leaf = segments[#segments]

		if block.isBlob then
			-- island: deserialize the subtree and parent it
			if not block.dataB64 then
				addWarning(labelPrefix .. ": island " .. block.pathText .. " has no !data")
				continue
			end
			local okDeser, listOrErr = pcall(function()
				return SerializationService:DeserializeInstancesAsync(
					buffer.fromstring(base64Decode(block.dataB64 :: string))
				)
			end)
			if not okDeser then
				addWarning(labelPrefix .. ": island " .. block.pathText .. " failed: " .. tostring(listOrErr))
				continue
			end
			for _, inst in listOrErr :: { Instance } do
				inst.Parent = parent
			end
			continue
		end

		-- reuse an existing non-creatable child (StarterPlayerScripts, Terrain, ...)
		local inst: Instance? = nil
		local existing = findOccurrence(parent, leaf.name, leaf.occurrence)
		if existing and existing.ClassName == block.className then
			local okNew, probe = pcall(function()
				return Instance.new(block.className)
			end)
			if okNew then
				(probe :: Instance):Destroy()
			else
				inst = existing -- non-creatable: reuse
			end
		end
		if not inst then
			local okNew, created = pcall(function()
				return Instance.new(block.className)
			end)
			if not okNew then
				addWarning(labelPrefix .. ": cannot create " .. block.className .. " at " .. block.pathText)
				continue
			end
			inst = created :: Instance
			inst.Name = leaf.name
			inst.Parent = parent
		end
		instanceByPath[block.pathText] = inst :: Instance
		applyProps(inst :: Instance, block, entryRoot, labelPrefix .. ":" .. block.pathText)
	end
end

------------------------------------------------------------------------------
-- Pass 1: build every entry
------------------------------------------------------------------------------

local appliedEntries = 0
local entryRoots: { [string]: Instance } = {}

for _, entry in CONFIG.entries do
	if entry.mode == "children" then
		-- sgfl save always writes the root file for a children-mode entry, so
		-- its absence means this entry was never saved — keep base content,
		-- exactly like the missing-entry-file skip below for "file" mode
		local rootFileName = entry.folder .. "/" .. entry.name .. ".sgfl"
		local rootText = inputFiles[rootFileName]
		if not rootText then
			continue
		end

		local root, resolveErr = resolveEntryRoot(entry.robloxPath)
		if not root then
			addWarning("entry " .. entry.name .. ": " .. resolveErr :: string)
			continue
		end
		entryRoots[entry.name] = root
		local isService = not entry.robloxPath:find("%.")
		local rootAbsolute = pathOfSegments(root)

		-- parse before clearing: a malformed entry file must abort the task
		-- before any existing content is destroyed
		local rootBlocks = parseEntryText(rootText, entry.name)

		clearEntryRoot(root, rootAbsolute, isService, entry)
		applyBlocks(rootBlocks, root, root, entry.name)

		-- child files aren't in CONFIG (names are dynamic) — discover them by
		-- folder prefix among the files actually shipped in this task's input
		local prefix = entry.folder .. "/"
		for fileName, data in inputFiles do
			if fileName == rootFileName or fileName:sub(1, #prefix) ~= prefix then
				continue
			end
			local rest = fileName:sub(#prefix + 1)
			if rest:sub(-10) == ".sgfl.rbxm" then
				local okDeser, listOrErr = pcall(function()
					return SerializationService:DeserializeInstancesAsync(buffer.fromstring(data))
				end)
				if okDeser then
					reparentBlobChildren(listOrErr :: { Instance }, root)
				else
					addWarning(entry.name .. ": child blob " .. fileName .. " deserialize failed: " .. tostring(
						listOrErr
					))
				end
			elseif rest:sub(-5) == ".sgfl" then
				local childName = rest:sub(1, #rest - 5)
				local childBlocks = parseEntryText(data, entry.name .. ":" .. childName)
				local rootBlock: Block? = nil
				for _, block in childBlocks do
					if block.pathText == "." and not block.isSidecar then
						rootBlock = block
						break
					end
				end
				if not rootBlock then
					addWarning(entry.name .. ": child file " .. fileName .. " missing root block")
					continue
				end
				local okNew, child = pcall(function()
					return Instance.new((rootBlock :: Block).className)
				end)
				if not okNew then
					addWarning(
						entry.name .. ": cannot create " .. (rootBlock :: Block).className .. " for child " .. childName
					)
					continue
				end
				local newChild = child :: Instance
				newChild.Name = childName
				newChild.Parent = root
				applyBlocks(childBlocks, newChild, newChild, entry.name .. ":" .. childName)
			end
		end

		appliedEntries += 1
		continue
	end

	local textName = entry.folder .. "/" .. entry.name .. ".sgfl"
	local text = inputFiles[textName]
	if not text then
		-- missing entry file = keep base content (matches legacy build.luau skip)
		continue
	end
	local root, resolveErr = resolveEntryRoot(entry.robloxPath)
	if not root then
		addWarning("entry " .. entry.name .. ": " .. resolveErr :: string)
		continue
	end
	entryRoots[entry.name] = root
	local isService = not entry.robloxPath:find("%.")
	local rootAbsolute = pathOfSegments(root)

	-- parse before clearing: a malformed entry file must abort the task
	-- before any existing content is destroyed
	local blocks = parseEntryText(text, entry.name)

	clearEntryRoot(root, rootAbsolute, isService, entry)
	applyBlocks(blocks, root, root, entry.name)

	-- NOTE: reused non-creatable children (TextChatService configs, Terrain,
	-- StarterPlayerScripts, ...) keep their engine-assigned position — their
	-- Parent is locked, so they cannot be reordered. The projection side
	-- compensates by emitting them in a canonical order (see
	-- canonicalChildren in projection_v2.luau), so saves stay stable.

	-- blob entry children
	local blobName = entry.folder .. "/" .. entry.name .. ".sgfl.rbxm"
	local blobData = inputFiles[blobName]
	if entry.format == "blob" and blobData then
		local okDeser, listOrErr = pcall(function()
			return SerializationService:DeserializeInstancesAsync(buffer.fromstring(blobData))
		end)
		if okDeser then
			reparentBlobChildren(listOrErr :: { Instance }, root)
		else
			addWarning(entry.name .. ": blob deserialize failed: " .. tostring(listOrErr))
		end
	end
	appliedEntries += 1
end

------------------------------------------------------------------------------
-- Pass 2: refs
------------------------------------------------------------------------------

local function resolveRef(refText: string, entryRoot: Instance): Instance?
	local segmentsText: string
	local cursor: Instance
	if refText:sub(1, 2) == "./" then
		cursor = entryRoot
		segmentsText = refText:sub(3)
	elseif refText:sub(1, 5) == "game/" then
		cursor = game
		segmentsText = refText:sub(6)
	else
		return nil
	end
	for _, segment in splitPath(segmentsText) do
		local nextInst = findOccurrence(cursor, segment.name, segment.occurrence)
		if not nextInst and cursor == game then
			local ok, service = pcall(function()
				return game:GetService(segment.name :: any)
			end)
			nextInst = if ok then service else nil
		end
		if not nextInst then
			return nil
		end
		cursor = nextInst
	end
	return cursor
end

local resolvedRefs, failedRefs = 0, 0
for _, pending in pendingRefs do
	local target = resolveRef(pending.refText, pending.entryRoot)
	if target then
		local okSet, setErr = pcall(function()
			(pending.holder :: any)[pending.propName] = target
		end)
		if okSet then
			resolvedRefs += 1
		else
			failedRefs += 1
			addWarning("ref set failed: " .. pending.propName .. " -> " .. pending.refText .. ": " .. tostring(setErr))
		end
	else
		failedRefs += 1
		addWarning("ref target not found: " .. pending.propName .. " -> " .. pending.refText)
	end
end

------------------------------------------------------------------------------
-- Post-apply hook + save
------------------------------------------------------------------------------

if POSTAPPLY then
	local okHook, hookErr = pcall(POSTAPPLY, game)
	if not okHook then
		error("postapply hook failed: " .. tostring(hookErr))
	end
end

local baseVersion = game.PlaceVersion
AssetService:SavePlaceAsync()

return {
	baseVersion = baseVersion,
	appliedEntries = appliedEntries,
	resolvedRefs = resolvedRefs,
	failedRefs = failedRefs,
	patchProps = patchProps,
	warningCount = #warnings,
	warnings = warnings,
}
