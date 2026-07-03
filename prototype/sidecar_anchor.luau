--[[
	sidecar_anchor.luau — returns script-readable Workspace property values to act
	as ground truth for validating the binary sidecar extractor: if the extractor
	decodes these to identical values from the place file, its decoding of the
	NotScriptable properties (which nothing else can read) is trustworthy.
]]

local anchors = {}
for _, propName in
	{
		"Gravity", -- Float32
		"FallenPartsDestroyHeight", -- Float32
		"StreamingEnabled", -- Bool
		"AirDensity", -- Float32
		"GlobalWind", -- Vector3 (reported for reference, not decoded by prototype)
		"ModelStreamingMode", -- Enum
		"Retargeting", -- Enum
		"ClientAnimatorThrottling", -- Enum
	}
do
	local ok, value = pcall(function()
		return (workspace :: any)[propName]
	end)
	if ok then
		local entry = { type = typeof(value), value = tostring(value) }
		if typeof(value) == "EnumItem" then
			entry.enumValue = value.Value
		end
		anchors[propName] = entry
	else
		anchors[propName] = { error = tostring(value) }
	end
end

return {
	placeVersion = game.PlaceVersion,
	anchors = anchors,
}
