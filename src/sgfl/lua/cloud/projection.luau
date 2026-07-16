--[[
	projection_v2.luau — the save-direction projector.

	For each configured assets.json entry, walks the subtree and emits either:
	  - a deterministic text projection (.sgfl), or
	  - a text companion for the root + an engine-serialized children blob
	    (.sgfl + .sgfl.rbxm) for blob-tier entries.

	Read-only: never calls SavePlaceAsync.

	Output: binary container returned as BinaryOutput:
	  [u32 manifestLen][manifest JSON][file data ...]
	manifest = { placeVersion, files = { {name, offset, size} }, warnings }

	The driver fills the CONFIG placeholder below (which must appear exactly
	once in this file — never write the token in comments) with JSON:
	  {
	    "entries": [ {"name","folder","robloxPath","format","mode","include","exclude"} ],
	                                                               -- format: "text"|"blob"; mode: "file"|"children"
	    "rojoPaths": [ "StarterPlayer/StarterPlayerScripts/Client", ... ],
	    "sidecarProps": [ "Workspace.StreamingMinRadius", ... ],  -- hidden props the binary sidecar covers
	    "anchorProps": [ "Gravity", ... ]                          -- readable Workspace props returned for the canary
	  }

	Text format (one block per instance, in child order):
	  [<path>] <ClassName>
	  <Prop> = <value>          -- sorted, non-default only (all props if class not creatable)
	  @Attr.<name> = <value>    -- sorted
	  @Tags = ["a", "b"]        -- sorted
	Root block path is ".". Paths are /-joined names, ":N" suffix for the Nth
	same-named sibling (1-based, omitted when N==1). Blob islands (hidden-state
	subtrees inside text entries):
	  [<path>] <ClassName> !blob
	  !data = <base64>

	mode = "children": the entry's direct children (filtered by include/exclude,
	same semantics as "file" mode but applied once at the entry root) are each
	projected as their own file under the entry's folder — {ChildName}.sgfl for
	text tier, {ChildName}.sgfl.rbxm (whole child as one engine blob) for blob
	tier — plus {Entry}.sgfl holding only the container root's own block.
	include/exclude entries are name globs ("Temp*") or "$ClassName" (IsA match);
	include-then-exclude precedence; unmanaged children are untouched by save.
]]

local HttpService = game:GetService("HttpService")
local ReflectionService = game:GetService("ReflectionService")
local SerializationService = game:GetService("SerializationService")
local CollectionService = game:GetService("CollectionService")

local CONFIG = HttpService:JSONDecode([===[__SGFL_CONFIG__]===])

local warnings = {}
local function addWarning(message: string)
	table.insert(warnings, message)
	print("WARN: " .. message)
end

------------------------------------------------------------------------------
-- Reflection + defaults caches
------------------------------------------------------------------------------

-- Full-capability enumeration: the default GetPropertiesOfClass hides
-- capability-gated serialized props entirely (Script.Source, Lighting.Technology,
-- SurfaceAppearance texture maps, MaterialService overrides, ...). Passing every
-- SecurityCapability makes the sweep complete, so gated props classify as
-- readable (text), hidden (island/sidecar), or benign — never invisible.
local REFLECTION_OPTS = (function()
	local ok, caps = pcall(function()
		return SecurityCapabilities.new(table.unpack(Enum.SecurityCapability:GetEnumItems()))
	end)
	if not ok then
		-- falling back to the default sweep would silently reintroduce the
		-- blind spot AND shift island/sidecar classification — fail loud instead
		error("full-capability reflection options failed: " .. tostring(caps) .. " — update sgfl", 0)
	end
	return { Security = caps }
end)()

-- Instance-level unreadable-serialized baseline (ignorable internals present on
-- every class: AttributesSerialize, HistoryId, UniqueId, Tags, ...)
local baselineUnreadable = {}
do
	local folder = Instance.new("Folder")
	local props = ReflectionService:GetPropertiesOfClass("Folder", REFLECTION_OPTS)
	for _, p in props do
		if p.Serialized then
			local ok = pcall(function()
				return (folder :: any)[p.Name]
			end)
			if not ok then
				baselineUnreadable[p.Name] = true
			end
		end
	end
end

-- Hidden props that are derivable/regenerable and therefore do NOT force a blob island
local BENIGN_HIDDEN = {
	Color3uint8 = true, -- backed by readable Color
	MaterialVariantSerialized = true, -- backed by readable MaterialVariant
	WorldPivotData = true, -- backed by readable WorldPivot
	ScaleFactor = true, -- Model:GetScale()
	Health_XML = true, -- backed by readable Health
	MaxFrictionTorqueXml = true, -- backed by readable MaxFrictionTorque
	ModelMeshData = true, -- regenerable cache
	ModelMeshCFrame = true,
	ModelMeshSize = true,
	ScriptGuid = true, -- regenerated
	formFactorRaw = true, -- legacy
	NeedsPivotMigration = true,
	CFrame0 = true, -- WeldConstraint internals, re-derived from Part0/Part1
	Part0Internal = true,
	Part1Internal = true,
	State = true,
	EnabledInternal = true,
	-- Studio editor/session state exposed by the full-capability sweep: never
	-- gameplay-affecting, and several are types the binary sidecar cannot patch.
	-- Deliberately NOT here (carried via sidecar instead): Workspace.AuthorityMode,
	-- TextChatService.EnableProtectedChat/IsLegacyChatDisabled, SoundService
	-- audio-API migration flags, MaterialService override names.
	LevelOfDetail = true, -- Studio render LOD (Model/Workspace/Tool)
	SlimHash = true, -- internal content hash
	LuauTypeCheckMode = true, -- Studio script-analysis mode
	PlayerModuleStatus = true, -- StarterPlayer internal
	LocalizationMatchIdentifier = true, -- localization autoscrape state
	LocalizationMatchedSourceText = true,
	ChatTranslationFTUXShown = true, -- TextChatService Studio-UX state
	ChatTranslationToggleEnabled = true,
	HasSeenDeprecationDialog = true,
	StudioDefaultStyleSheet = true, -- StarterGui Studio editor refs (Ref-typed)
	StudioInsertWidgetLayerCollectorAutoLinkStyleSheet = true,
	InternalVideoUsage = true, -- VideoFrame Roblox-managed state (not user-settable)
	MaximumResolution = true,
}

-- Prefix-matched benign-hidden names (see BENIGN_HIDDEN)
local BENIGN_HIDDEN_PREFIXES = {
	"GameSettings", -- StarterPlayer avatar-testing overrides (Studio Test tab)
}

local function isBenignHidden(name: string): boolean
	if BENIGN_HIDDEN[name] then
		return true
	end
	for _, prefix in BENIGN_HIDDEN_PREFIXES do
		if name:sub(1, #prefix) == prefix then
			return true
		end
	end
	return false
end

-- serialized-but-junk properties: session/runtime state we deliberately drop
local SKIP_PROPS: { [string]: { [string]: boolean } } = {
	Workspace = { CurrentCamera = true, DistributedGameTime = true },
}

type PropInfo = { name: string, readable: boolean }
local classProps: { [string]: { PropInfo } } = {}
local classHiddenState: { [string]: { string } } = {} -- props forcing blob island
local classDefaults: { [string]: { [string]: any } | false } = {}

-- Some serialized member names the reflection sweep reports are deprecated
-- lowercase aliases (Part.size, Part.shape, ...) whose getters still work but
-- whose setters are gone — projecting them makes apply's write throw. When the
-- capitalized modern name reads back the same value, project that instead.
local function canonicalPropName(inst: Instance, name: string, value: any): string
	if not name:sub(1, 1):match("%l") then
		return name
	end
	local canonical = name:sub(1, 1):upper() .. name:sub(2)
	local ok, sameValue = pcall(function()
		return (inst :: any)[canonical] == value
	end)
	if ok and sameValue == true then
		return canonical
	end
	return name
end

local function propsOf(inst: Instance): { PropInfo }
	local className = inst.ClassName
	if classProps[className] then
		return classProps[className]
	end
	local skip = SKIP_PROPS[className]
	local out: { PropInfo } = {}
	local hidden: { string } = {}
	local seen: { [string]: boolean } = {}
	local okProps, props = pcall(function()
		return ReflectionService:GetPropertiesOfClass(className, REFLECTION_OPTS)
	end)
	if okProps and props then
		for _, p in props do
			if not p.Serialized or p.Name == "Parent" or p.Name == "Name" then
				continue
			end
			if skip and skip[p.Name] then
				continue
			end
			local okRead, value = pcall(function()
				return (inst :: any)[p.Name]
			end)
			if okRead then
				local name = canonicalPropName(inst, p.Name, value)
				if not seen[name] then
					seen[name] = true
					table.insert(out, { name = name, readable = true })
				end
			elseif not baselineUnreadable[p.Name] and not isBenignHidden(p.Name) then
				table.insert(hidden, p.Name)
			end
		end
	else
		addWarning(className .. ": GetPropertiesOfClass failed: " .. tostring(props))
	end
	table.sort(out, function(a, b)
		return a.name < b.name
	end)
	classProps[className] = out
	classHiddenState[className] = hidden
	return out
end

local function hiddenStateOf(inst: Instance): { string }
	propsOf(inst)
	return classHiddenState[inst.ClassName]
end

local function defaultsOf(className: string): { [string]: any } | false
	local cached = classDefaults[className]
	if cached ~= nil then
		return cached
	end
	local okNew, fresh = pcall(function()
		return Instance.new(className)
	end)
	if not okNew then
		classDefaults[className] = false
		return false
	end
	local defaults = {}
	for _, p in propsOf(fresh) do
		local ok, v = pcall(function()
			return (fresh :: any)[p.name]
		end)
		if ok then
			defaults[p.name] = v
		end
	end
	fresh:Destroy()
	classDefaults[className] = defaults
	return defaults
end

------------------------------------------------------------------------------
-- Paths
------------------------------------------------------------------------------

local function escapeSegment(name: string): string
	local out = name:gsub("\\", "\\\\")
		:gsub("/", "\\/")
		:gsub(":", "\\:")
		:gsub("\n", "\\n")
		:gsub("\r", "\\r")
		:gsub("\t", "\\t")
	return out
end

local function occurrenceSegment(inst: Instance): string
	local parent = inst.Parent
	local segment = escapeSegment(inst.Name)
	if not parent then
		return segment
	end
	local index = 0
	for _, sibling in parent:GetChildren() do
		if sibling.Name == inst.Name then
			index += 1
			if sibling == inst then
				break
			end
		end
	end
	if index > 1 then
		return segment .. ":" .. index
	end
	return segment
end

-- absolute path segments from game (nil if not under game)
local function absoluteSegments(inst: Instance): { string }?
	local segments = {}
	local cursor: Instance? = inst
	while cursor and cursor ~= game do
		table.insert(segments, 1, occurrenceSegment(cursor))
		cursor = cursor.Parent
	end
	if cursor ~= game then
		return nil
	end
	return segments
end

------------------------------------------------------------------------------
-- Value encoding
------------------------------------------------------------------------------

local BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
local function base64Encode(data: string): string
	local out = table.create(math.ceil(#data / 3))
	for i = 1, #data, 3 do
		local a, b, c = string.byte(data, i, i + 2)
		local n = a * 65536 + (b or 0) * 256 + (c or 0)
		local chunk = string.sub(BASE64_ALPHABET, bit32.extract(n, 18, 6) + 1, bit32.extract(n, 18, 6) + 1)
			.. string.sub(BASE64_ALPHABET, bit32.extract(n, 12, 6) + 1, bit32.extract(n, 12, 6) + 1)
			.. (if b then string.sub(BASE64_ALPHABET, bit32.extract(n, 6, 6) + 1, bit32.extract(n, 6, 6) + 1) else "=")
			.. (if c then string.sub(BASE64_ALPHABET, bit32.extract(n, 0, 6) + 1, bit32.extract(n, 0, 6) + 1) else "=")
		table.insert(out, chunk)
	end
	return table.concat(out)
end

local numberScratch = buffer.create(4)
local function roundtripsF32(v: number): boolean
	buffer.writef32(numberScratch, 0, v)
	return buffer.readf32(numberScratch, 0) == v
end

local function encodeNumber(v: number): string
	if v ~= v then
		return "nan"
	end
	if v == math.huge then
		return "inf"
	end
	if v == -math.huge then
		return "-inf"
	end
	if v == math.floor(v) and math.abs(v) < 2 ^ 53 then
		return string.format("%.0f", v)
	end
	-- f32-backed values (the overwhelming majority) get the shortest string
	-- that survives an f32 round-trip; true doubles get an f64 round-trip
	if roundtripsF32(v) then
		for precision = 1, 9 do
			local s = string.format("%." .. precision .. "g", v)
			local parsed = tonumber(s)
			if parsed then
				buffer.writef32(numberScratch, 0, parsed)
				if buffer.readf32(numberScratch, 0) == v then
					return s
				end
			end
		end
	end
	for precision = 10, 17 do
		local s = string.format("%." .. precision .. "g", v)
		if tonumber(s) == v then
			return s
		end
	end
	return string.format("%.17g", v)
end

local function encodeString(v: string): string
	-- multi-line -> heredoc handled by caller; this is the single-line quoted form
	local out = v:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t")
	-- escape remaining control chars
	out = out:gsub("%c", function(c)
		return string.format("\\x%02X", string.byte(c))
	end)
	return '"' .. out .. '"'
end

local function numbers(...): string
	local parts = {}
	for _, v in { ... } do
		table.insert(parts, encodeNumber(v))
	end
	return table.concat(parts, ", ")
end

local encodeValue -- forward declaration

local refEncoder: (Instance) -> string -- set per-entry during projection

local valueEncoders: { [string]: (any) -> string } = {
	boolean = function(v)
		return tostring(v)
	end,
	number = function(v)
		return encodeNumber(v)
	end,
	string = function(v)
		return encodeString(v)
	end,
	EnumItem = function(v)
		return "Enum." .. tostring(v.EnumType) .. "." .. v.Name
	end,
	Color3 = function(v)
		return "Color3(" .. numbers(v.R, v.G, v.B) .. ")"
	end,
	Vector2 = function(v)
		return "Vector2(" .. numbers(v.X, v.Y) .. ")"
	end,
	Vector3 = function(v)
		return "Vector3(" .. numbers(v.X, v.Y, v.Z) .. ")"
	end,
	Vector2int16 = function(v)
		return "Vector2int16(" .. numbers(v.X, v.Y) .. ")"
	end,
	Vector3int16 = function(v)
		return "Vector3int16(" .. numbers(v.X, v.Y, v.Z) .. ")"
	end,
	CFrame = function(v)
		local components = { v:GetComponents() }
		return "CFrame(" .. numbers(table.unpack(components)) .. ")"
	end,
	UDim = function(v)
		return "UDim(" .. numbers(v.Scale, v.Offset) .. ")"
	end,
	UDim2 = function(v)
		return "UDim2(" .. numbers(v.X.Scale, v.X.Offset, v.Y.Scale, v.Y.Offset) .. ")"
	end,
	Rect = function(v)
		return "Rect(" .. numbers(v.Min.X, v.Min.Y, v.Max.X, v.Max.Y) .. ")"
	end,
	NumberRange = function(v)
		return "NumberRange(" .. numbers(v.Min, v.Max) .. ")"
	end,
	BrickColor = function(v)
		return "BrickColor(" .. v.Number .. ")"
	end,
	NumberSequence = function(v)
		local parts = {}
		for _, kp in v.Keypoints do
			table.insert(parts, "(" .. numbers(kp.Time, kp.Value, kp.Envelope) .. ")")
		end
		return "NumberSequence[" .. table.concat(parts, ", ") .. "]"
	end,
	ColorSequence = function(v)
		local parts = {}
		for _, kp in v.Keypoints do
			table.insert(parts, "(" .. numbers(kp.Time, kp.Value.R, kp.Value.G, kp.Value.B) .. ")")
		end
		return "ColorSequence[" .. table.concat(parts, ", ") .. "]"
	end,
	PhysicalProperties = function(v)
		return "PhysicalProperties("
			.. numbers(v.Density, v.Friction, v.Elasticity, v.FrictionWeight, v.ElasticityWeight)
			.. ")"
	end,
	Font = function(v)
		return "Font(" .. encodeString(v.Family) .. ", " .. v.Weight.Name .. ", " .. v.Style.Name .. ")"
	end,
	Faces = function(v)
		local names = {}
		for _, face in Enum.NormalId:GetEnumItems() do
			if v[face.Name] then
				table.insert(names, face.Name)
			end
		end
		return "Faces(" .. table.concat(names, ", ") .. ")"
	end,
	Axes = function(v)
		local names = {}
		for _, axis in { "X", "Y", "Z" } do
			if v[axis] then
				table.insert(names, axis)
			end
		end
		return "Axes(" .. table.concat(names, ", ") .. ")"
	end,
	Content = function(v)
		if v.SourceType == Enum.ContentSourceType.Uri then
			return "Content(" .. encodeString(v.Uri) .. ")"
		elseif v.SourceType == Enum.ContentSourceType.None then
			return "Content()"
		end
		error("Content with SourceType " .. tostring(v.SourceType) .. " cannot be projected")
	end,
	Instance = function(v)
		return refEncoder(v)
	end,
	SecurityCapabilities = function(v)
		-- no Luau API reads the bits; only the empty default is encodable
		local fresh = Instance.new("Folder")
		local default = fresh.Capabilities
		fresh:Destroy()
		if tostring(v) == tostring(default) then
			return "SecurityCapabilities()"
		end
		error("non-default SecurityCapabilities cannot be projected")
	end,
}

encodeValue = function(v: any): string
	local t = typeof(v)
	local encoder = valueEncoders[t]
	if not encoder then
		error("no encoder for type " .. t)
	end
	return encoder(v)
end

------------------------------------------------------------------------------
-- Text projection
------------------------------------------------------------------------------

local function encodePropLine(name: string, value: any): string
	if typeof(value) == "string" and (value:find("\n") or #value > 200) then
		-- heredoc: one raw line per file line, each prefixed with a tab
		local lines = value:split("\n")
		local out = { name .. " = <<<" }
		for _, line in lines do
			table.insert(out, "\t" .. line)
		end
		table.insert(out, ">>>")
		return table.concat(out, "\n")
	end
	return name .. " = " .. encodeValue(value)
end

-- hidden props the binary sidecar takes care of (never force an island)
local sidecarCovered = {}
for _, key in (CONFIG.sidecarProps or {}) :: { string } do
	sidecarCovered[key] = true
end

-- class-specific handling: hidden serialized props that are fully recoverable
-- through a readable API (so no island is needed), plus the extra projection
-- lines that capture the API-visible state
type SpecialHandler = { benignHidden: { [string]: boolean }, extra: (Instance, { string }) -> () }
local SPECIAL: { [string]: SpecialHandler }

-- hidden serialized props that actually force a blob island for this instance
local function islandProps(inst: Instance): { string }
	local out = {}
	local special = SPECIAL[inst.ClassName]
	for _, name in hiddenStateOf(inst) do
		if sidecarCovered[inst.ClassName .. "." .. name] then
			continue
		end
		if special and special.benignHidden[name] then
			continue
		end
		table.insert(out, name)
	end
	return out
end

SPECIAL = {
	StyleRule = {
		-- PropertiesSerialize is fully recoverable via GetProperties/SetProperties.
		-- PropertyTransitionsSerialize is guarded below: the API is FFlag-gated
		-- ("Feature not enabled yet"), so no user data can exist in it; the day
		-- transitions go live and a rule has any, projection fails loudly.
		benignHidden = { PropertiesSerialize = true, PropertyTransitionsSerialize = true },
		extra = function(inst, out)
			local rule = inst :: any
			local props = rule:GetProperties()
			local names = {}
			for name in props do
				table.insert(names, name)
			end
			table.sort(names)
			for _, name in names do
				table.insert(out, encodePropLine("@Style." .. name, props[name]))
			end
			local okTransitions, transitions = pcall(function()
				return rule:GetPropertyTransitions()
			end)
			if okTransitions then
				for _ in transitions do
					error(inst:GetFullName() .. ": StyleRule property transitions are not supported yet")
				end
			end
		end,
	},
}

local function projectInstance(inst: Instance, pathText: string, out: { string }, isRoot: boolean?)
	local className = inst.ClassName
	local hidden = islandProps(inst)

	if #hidden > 0 and isRoot then
		-- entry roots (services) cannot be islands; their hidden props are
		-- reported via the manifest so the driver's binary sidecar can carry
		-- them (see hiddenRoots below)
	elseif #hidden > 0 then
		-- must-blob: serialize this whole subtree as an inline island
		local okBlob, blob = pcall(function()
			return SerializationService:SerializeInstancesAsync({ inst })
		end)
		if not okBlob then
			error(
				pathText
					.. " ("
					.. className
					.. ") has hidden state ["
					.. table.concat(hidden, ", ")
					.. "] and cannot be blob-serialized: "
					.. tostring(blob)
			)
		end
		table.insert(out, "[" .. pathText .. "] " .. className .. " !blob")
		table.insert(out, "!hidden = " .. table.concat(hidden, ", "))
		table.insert(out, "!data = " .. base64Encode(buffer.tostring(blob)))
		table.insert(out, "")
		return -- descendants are inside the blob
	end

	table.insert(out, "[" .. pathText .. "] " .. className)

	local defaults = defaultsOf(className)
	for _, p in propsOf(inst) do
		local okRead, value = pcall(function()
			return (inst :: any)[p.name]
		end)
		if not okRead then
			continue -- readability probed per-class on a representative; instance-level failure is unexpected
		end
		if defaults and defaults[p.name] ~= nil then
			local default = defaults[p.name]
			local same = value == default
			if not same and typeof(value) == typeof(default) and typeof(value) ~= "Instance" then
				-- value-type userdata compare by encoding when == is identity-based
				local okEnc, encoded = pcall(encodeValue, value)
				local okEncD, encodedDefault = pcall(encodeValue, default)
				same = okEnc and okEncD and encoded == encodedDefault
			end
			if same then
				continue
			end
		end
		if value == nil then
			continue
		end
		local okLine, line = pcall(encodePropLine, p.name, value)
		if okLine then
			table.insert(out, line)
		else
			addWarning(pathText .. "." .. p.name .. ": " .. tostring(line))
		end
	end

	-- class-specific API-recovered state (e.g. StyleRule styled properties)
	local special = SPECIAL[className]
	if special then
		special.extra(inst, out)
	end

	-- attributes (sorted). Service roots are special: their attributes
	-- include Roblox-internal RBX_* migration state that is CoreScript-gated
	-- on write yet load-bearing (e.g. Lighting's technology migration flags),
	-- so the whole AttributesSerialize blob rides the binary sidecar instead
	-- of the text projection.
	local attrNames = {}
	local attrs = if isRoot and inst.Parent == game then {} else inst:GetAttributes()
	for name in attrs do
		if name:sub(1, 4) ~= "RBX_" then
			table.insert(attrNames, name)
		end
	end
	table.sort(attrNames)
	for _, name in attrNames do
		local okLine, line = pcall(encodePropLine, "@Attr." .. name, attrs[name])
		if okLine then
			table.insert(out, line)
		else
			addWarning(pathText .. " attribute " .. name .. ": " .. tostring(line))
		end
	end

	-- tags (sorted)
	local tags = CollectionService:GetTags(inst)
	if #tags > 0 then
		table.sort(tags)
		local quoted = {}
		for _, tag in tags do
			table.insert(quoted, encodeString(tag))
		end
		table.insert(out, "@Tags = [" .. table.concat(quoted, ", ") .. "]")
	end

	table.insert(out, "")
end

------------------------------------------------------------------------------
-- Entry projection
------------------------------------------------------------------------------

local rojoExcluded = {} -- set of "Service/Child/..." absolute path strings
for _, p in (CONFIG.rojoPaths or {}) :: { string } do
	rojoExcluded[p] = true
end

------------------------------------------------------------------------------
-- Entry filters: include/exclude name globs ("Temp*") or "$ClassName" (IsA)
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

local function isValidChildFilename(name: string): boolean
	if name == "" then
		return false
	end
	if name:match("^[%s%.]") or name:match("[%s%.]$") then
		return false
	end
	return name:match("^[%w _%.%-]+$") ~= nil
end

local function resolveEntryRoot(robloxPath: string): (Instance?, string?)
	local segments = robloxPath:split(".")
	local service = game:FindFirstChild(segments[1])
	if not service then
		local ok, fetched = pcall(function()
			return game:GetService(segments[1])
		end)
		service = ok and fetched or nil
	end
	if not service then
		return nil, "service " .. segments[1] .. " not found"
	end
	local cursor = service
	for i = 2, #segments do
		local nextChild = cursor:FindFirstChild(segments[i])
		if not nextChild then
			return nil, robloxPath .. ' not found (missing "' .. segments[i] .. '")'
		end
		cursor = nextChild
	end
	return cursor, nil
end

-- children in canonical projection order: non-creatable singletons first
-- (sorted by ClassName then Name) since their live position is engine
-- history the apply side cannot reproduce (their Parent is locked), then
-- everything else in document order
local function canonicalChildren(inst: Instance): { Instance }
	local locked, normal = {}, {}
	for _, child in inst:GetChildren() do
		if defaultsOf(child.ClassName) == false then
			table.insert(locked, child)
		else
			table.insert(normal, child)
		end
	end
	table.sort(locked, function(a, b)
		if a.ClassName ~= b.ClassName then
			return a.ClassName < b.ClassName
		end
		return a.Name < b.Name
	end)
	for _, child in normal do
		table.insert(locked, child)
	end
	return locked
end

local function projectEntryText(root: Instance, rootAbsolute: { string }, entry, filterAtRoot: boolean): string
	local out = {}
	local entryRoot = root

	refEncoder = function(target: Instance): string
		local segments = absoluteSegments(target)
		if not segments then
			error("ref target " .. target:GetFullName() .. " is not under the DataModel")
		end
		-- inside this entry? -> relative
		local cursor: Instance? = target
		local relative = {}
		while cursor and cursor ~= entryRoot do
			table.insert(relative, 1, occurrenceSegment(cursor))
			cursor = cursor.Parent
		end
		if cursor == entryRoot then
			return "Ref(./" .. table.concat(relative, "/") .. ")"
		end
		return "Ref(game/" .. table.concat(segments, "/") .. ")"
	end

	projectInstance(root, ".", out, true)

	local function visit(inst: Instance, pathText: string, absolute: { string }, applyFilter: boolean)
		for _, child in canonicalChildren(inst) do
			-- non-Archivable = engine-injected session-only content (default
			-- PlayerModule etc.); no serializer persists it and neither do we
			if not child.Archivable then
				continue
			end
			-- Camera is stripped (regenerated by Studio), matching legacy importAssets
			if child:IsA("Camera") and inst == workspace then
				continue
			end
			-- filters apply only to direct children of the entry root
			if applyFilter and not isManaged(child, entry) then
				continue
			end
			local segment = occurrenceSegment(child)
			local childAbsolute = table.clone(absolute)
			table.insert(childAbsolute, segment)
			if rojoExcluded[table.concat(childAbsolute, "/")] then
				continue
			end
			local childPath = if pathText == "." then segment else pathText .. "/" .. segment
			projectInstance(child, childPath, out)
			-- islands embed their descendants; skip recursion for them
			if #islandProps(child) == 0 then
				visit(child, childPath, childAbsolute, false)
			end
		end
	end
	visit(root, ".", rootAbsolute, filterAtRoot)

	return table.concat(out, "\n")
end

local function projectEntryBlob(root: Instance, entry): (string, buffer?)
	-- text companion: the root's own properties only
	local out = {}
	refEncoder = function(target: Instance): string
		local segments = absoluteSegments(target)
		if not segments then
			error("ref target " .. target:GetFullName() .. " is not under the DataModel")
		end
		return "Ref(game/" .. table.concat(segments, "/") .. ")"
	end
	projectInstance(root, ".", out, true)

	local children = {}
	for _, child in canonicalChildren(root) do
		if not child.Archivable then
			continue
		end
		if child:IsA("Camera") and root == workspace then
			continue
		end
		if not isManaged(child, entry) then
			continue
		end
		table.insert(children, child)
	end
	if #children == 0 then
		return table.concat(out, "\n"), nil
	end
	local okBlob, blob = pcall(function()
		return SerializationService:SerializeInstancesAsync(children)
	end)
	if not okBlob then
		-- likely a rejected non-creatable child (e.g. Terrain); retry without it
		local rejected = {}
		local retryChildren = {}
		for _, child in children do
			local okOne = pcall(function()
				return SerializationService:SerializeInstancesAsync({ child })
			end)
			if okOne then
				table.insert(retryChildren, child)
			else
				table.insert(rejected, child.ClassName .. " " .. child.Name)
			end
		end
		addWarning(
			root:GetFullName()
				.. " children blob rejected ["
				.. tostring(blob)
				.. "]; excluded: "
				.. table.concat(rejected, ", ")
		)
		blob = SerializationService:SerializeInstancesAsync(retryChildren)
	end
	return table.concat(out, "\n"), blob :: buffer
end

-- mode = "children": container root's own block + one file per managed
-- direct child (text tier: full subtree via projectEntryText with the child
-- itself as the ref-relative root; blob tier: whole child as one engine blob)
local function projectEntryChildren(
	entry,
	root: Instance,
	rootAbsolute: { string }
): { { name: string, data: string } }
	local out: { { name: string, data: string } } = {}

	refEncoder = function(target: Instance): string
		local segments = absoluteSegments(target)
		if not segments then
			error("ref target " .. target:GetFullName() .. " is not under the DataModel")
		end
		return "Ref(game/" .. table.concat(segments, "/") .. ")"
	end
	local rootOut = {}
	projectInstance(root, ".", rootOut, true)
	table.insert(out, { name = entry.folder .. "/" .. entry.name .. ".sgfl", data = table.concat(rootOut, "\n") })

	local seenNames: { [string]: boolean } = {}
	for _, child in canonicalChildren(root) do
		if not child.Archivable then
			continue
		end
		if child:IsA("Camera") and root == workspace then
			continue
		end
		local childAbsolute = table.clone(rootAbsolute)
		table.insert(childAbsolute, occurrenceSegment(child))
		if rojoExcluded[table.concat(childAbsolute, "/")] then
			continue
		end
		if not isManaged(child, entry) then
			continue
		end

		if not isValidChildFilename(child.Name) then
			error(
				entry.name
					.. ': child name "'
					.. child.Name
					.. '" is not filesystem-safe (letters, digits, spaces, "_", ".", "-" only; no leading/trailing dot or space) — rename it in Studio'
			)
		end
		local lowerName = child.Name:lower()
		if seenNames[lowerName] then
			error(
				entry.name
					.. ': duplicate child name "'
					.. child.Name
					.. '" (case-insensitive) — rename one in Studio'
			)
		end
		seenNames[lowerName] = true

		if entry.format == "blob" then
			local okBlob, blob = pcall(function()
				return SerializationService:SerializeInstancesAsync({ child })
			end)
			if not okBlob then
				error(entry.name .. ': child "' .. child.Name .. '" cannot be blob-serialized: ' .. tostring(blob))
			end
			table.insert(
				out,
				{ name = entry.folder .. "/" .. child.Name .. ".sgfl.rbxm", data = buffer.tostring(blob :: buffer) }
			)
		else
			local childText = projectEntryText(child, childAbsolute, entry, false)
			table.insert(out, { name = entry.folder .. "/" .. child.Name .. ".sgfl", data = childText })
		end
	end

	return out
end

------------------------------------------------------------------------------
-- Main
------------------------------------------------------------------------------

local files: { { name: string, data: string } } = {}
local hiddenRoots: { [string]: { className: string, props: { string } } } = {}

for _, entry in CONFIG.entries do
	local root, resolveErr = resolveEntryRoot(entry.robloxPath)
	if not root then
		addWarning("entry " .. entry.name .. ": " .. resolveErr :: string)
		continue
	end
	local rootAbsolute = absoluteSegments(root) or {}

	-- report hidden serialized props of service-level roots so the driver's
	-- binary sidecar can capture them (single-instance classes only)
	if not entry.robloxPath:find("%.") then
		local hidden = hiddenStateOf(root)
		if #hidden > 0 then
			hiddenRoots[entry.name] = { className = root.ClassName, props = hidden }
		end
	end

	if entry.mode == "children" then
		local okChildren, filesOrErr = pcall(projectEntryChildren, entry, root, rootAbsolute)
		if not okChildren then
			addWarning("entry " .. entry.name .. " (children): " .. tostring(filesOrErr))
			continue
		end
		for _, f in filesOrErr :: { { name: string, data: string } } do
			table.insert(files, f)
		end
	elseif entry.format == "blob" then
		local okBlob, textOrErr, blob = pcall(projectEntryBlob, root, entry)
		if not okBlob then
			addWarning("entry " .. entry.name .. " (blob): " .. tostring(textOrErr))
			continue
		end
		table.insert(files, { name = entry.folder .. "/" .. entry.name .. ".sgfl", data = textOrErr :: string })
		if blob then
			table.insert(
				files,
				{ name = entry.folder .. "/" .. entry.name .. ".sgfl.rbxm", data = buffer.tostring(blob :: buffer) }
			)
		end
	else
		local okText, textOrErr = pcall(projectEntryText, root, rootAbsolute, entry, true)
		if not okText then
			addWarning("entry " .. entry.name .. " (text): " .. tostring(textOrErr))
			continue
		end
		table.insert(files, { name = entry.folder .. "/" .. entry.name .. ".sgfl", data = textOrErr })
	end
end

-- pack container
local manifestFiles = {}
local offset = 0
local dataParts = {}
for _, file in files do
	table.insert(manifestFiles, { name = file.name, offset = offset, size = #file.data })
	table.insert(dataParts, file.data)
	offset += #file.data
end
local manifest = HttpService:JSONEncode({
	placeVersion = game.PlaceVersion,
	files = manifestFiles,
	warnings = warnings,
	hiddenRoots = hiddenRoots,
})
local data = table.concat(dataParts)
local container = buffer.create(4 + #manifest + #data)
buffer.writeu32(container, 0, #manifest)
buffer.writestring(container, 4, manifest)
buffer.writestring(container, 4 + #manifest, data)

-- readable anchor values for the sidecar decoder canary (EnumItems as numbers)
local anchors = {}
for _, propName in (CONFIG.anchorProps or {}) :: { string } do
	local ok, value = pcall(function()
		return (workspace :: any)[propName]
	end)
	if ok then
		anchors[propName] = if typeof(value) == "EnumItem" then value.Value else value
	end
end

return {
	BinaryOutput = container,
	ReturnValues = {
		{
			placeVersion = game.PlaceVersion,
			fileCount = #files,
			totalBytes = buffer.len(container),
			warningCount = #warnings,
			anchors = anchors,
		},
	},
}
