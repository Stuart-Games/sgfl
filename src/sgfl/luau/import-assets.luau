local roblox = require("@lune/roblox")
local fs = require("@lune/fs")

local content = fs.readFile("WaterPhysics.rbxlx")
local game = roblox.deserializePlace(content)

--[[
	The camera is destroyed for two reasons:

	1. Camera pos and focus can cause merge conflicts
	2. Lune build creates a new camera anyways
]]
local camera = game.Workspace:FindFirstChild("Camera") :: Camera?
if camera then
	camera:Destroy()
end

if not fs.isDir("./map") then
	fs.writeDir("./map")
end

if not fs.isDir("./assets") then
	fs.writeDir("./assets")
end

if not fs.isDir("./gui") then
	fs.writeDir("./gui")
end

fs.writeFile("./map/Workspace.rbxmx", roblox.serializeModel({ game.Workspace }, true))
fs.writeFile("./map/Lighting.rbxmx", roblox.serializeModel({ game.Lighting }, true))
fs.writeFile("./assets/Materials.rbxmx", roblox.serializeModel({ game.MaterialService }, true))
fs.writeFile("./gui/StarterGui.rbxmx", roblox.serializeModel({ game.StarterGui }, true))
fs.writeFile("./assets/ReplicatedAssets.rbxmx", roblox.serializeModel({ game.ReplicatedStorage.Assets }, true))
fs.writeFile("./assets/ServerAssets.rbxmx", roblox.serializeModel({ game.ServerStorage.Assets }, true))
fs.writeFile("./assets/RemoteSignals.rbxmx", roblox.serializeModel({ game.ReplicatedStorage.RemoteSignals }, true))
