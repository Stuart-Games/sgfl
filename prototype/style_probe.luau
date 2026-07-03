--[[
	style_probe.luau — determines whether StyleRule and MaterialVariant hidden
	serialized props are fully recoverable via readable APIs (making them safe
	to project as text rather than blob islands).

	Runs against WaterPhysics (has real StyleSheets + MaterialVariants).
	Read-only.
]]

local results = {}

-- find a real StyleRule with styled properties
local styleRule: Instance? = nil
local styleSheet: Instance? = nil
for _, inst in game:GetDescendants() do
	if inst.ClassName == "StyleRule" and not styleRule then
		styleRule = inst
	elseif inst.ClassName == "StyleSheet" and not styleSheet then
		styleSheet = inst
	end
	if styleRule and styleSheet then
		break
	end
end

if styleRule then
	local out: { [string]: any } = { path = styleRule:GetFullName() }
	local rule = styleRule :: any

	local okProps, props = pcall(function()
		return rule:GetProperties()
	end)
	if okProps then
		local rendered = {}
		for name, value in props do
			rendered[name] = typeof(value) .. "|" .. tostring(value)
		end
		out.getProperties = rendered
	else
		out.getProperties = "ERR: " .. tostring(props)
	end

	for _, methodName in { "GetPropertyTransitions", "GetTransitions", "GetAttributesSerialize" } do
		local okMethod, value = pcall(function()
			return rule[methodName](rule)
		end)
		if okMethod then
			local rendered = {}
			for name, v in value do
				rendered[name] = typeof(v) .. "|" .. tostring(v)
			end
			out[methodName] = rendered
			out[methodName .. "_count"] = (function()
				local n = 0
				for _ in value do
					n += 1
				end
				return n
			end)()
		else
			out[methodName] = "ERR: " .. tostring(value)
		end
	end

	-- does SetProperties exist (apply path)?
	out.setPropertiesType = typeof(rule.SetProperties)
	out.setPropertyType = typeof(rule.SetProperty)

	results.styleRule = out
end

-- MaterialVariant: is TexturePack recoverable?
local variant: Instance? = nil
for _, inst in game:GetService("MaterialService"):GetChildren() do
	if inst.ClassName == "MaterialVariant" then
		variant = inst
		break
	end
end
if variant then
	local out: { [string]: any } = { path = variant:GetFullName() }
	local mv = variant :: any
	for _, name in { "TexturePack", "TexturePackId", "GetTexturePack", "ColorMap", "NormalMap", "MetalnessMap", "RoughnessMap" } do
		local okRead, value = pcall(function()
			return mv[name]
		end)
		out["read_" .. name] = okRead and (typeof(value) .. "|" .. tostring(value)) or ("ERR: " .. tostring(value))
	end
	-- fresh MaterialVariant: is TexturePack serialized on a fresh one too (i.e.
	-- always-serialized noise) — check blob size difference
	local SerializationService = game:GetService("SerializationService")
	local blobReal = SerializationService:SerializeInstancesAsync({ variant })
	local fresh = Instance.new("MaterialVariant")
	local blobFresh = SerializationService:SerializeInstancesAsync({ fresh })
	out.realBlobBytes = buffer.len(blobReal)
	out.freshBlobBytes = buffer.len(blobFresh)
	results.materialVariant = out
end

-- StyleSheet itself: any hidden props? (projected clean earlier, confirm attributes)
if styleSheet then
	results.styleSheet = {
		path = styleSheet:GetFullName(),
		attributes = styleSheet:GetAttributes(),
	}
end

return results
