--[[
	injected_probe.luau — identifies engine-injected (session-only) instances
	under StarterPlayer containers so projection can exclude them.
	Read-only.
]]

local StarterPlayer = game:GetService("StarterPlayer")

local results = {}
for _, containerName in { "StarterPlayerScripts", "StarterCharacterScripts" } do
	local container = StarterPlayer:FindFirstChild(containerName)
	if not container then
		results[containerName] = "missing"
		continue
	end
	local children = {}
	for _, child in container:GetChildren() do
		table.insert(children, {
			name = child.Name,
			className = child.ClassName,
			archivable = child.Archivable,
		})
	end
	results[containerName] = children
end

-- also check Workspace/Camera archivable state for reference
local camera = workspace:FindFirstChildOfClass("Camera")
results.cameraArchivable = camera and camera.Archivable or "no camera"

return results
