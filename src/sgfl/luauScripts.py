build = '''local roblox = require("@lune/roblox") :: any
local fs = require("@lune/fs") :: any
local Instance = roblox.Instance

function readAsset(filePath: string)
	local asset = roblox.deserializeModel(fs.readFile(filePath))[1]
	return asset
end

function readFileToParent(filePath: string, parent: Instance)
	local masterModel = readAsset(filePath)

	for i, child in masterModel:GetChildren() do
		child.Parent = parent
	end
end

--Services that are recorded directly
local Workspace = readAsset("./map/Workspace.rbxmx")
local Lighting = readAsset("./map/Lighting.rbxmx")
local MaterialService = readAsset("./assets/Materials.rbxmx")
local StarterGui = readAsset("./gui/StarterGui.rbxmx")

--Assets
local ReplicatedStorage = Instance.new("ReplicatedStorage")
local ServerStorage = Instance.new("ServerStorage")

local repAssetsFolder = Instance.new("Folder")
repAssetsFolder.Name = "Assets"
repAssetsFolder.Parent = ReplicatedStorage
readFileToParent("./assets/ReplicatedAssets.rbxmx", repAssetsFolder)

local serverAssetsFolder = Instance.new("Folder")
serverAssetsFolder.Name = "Assets"
serverAssetsFolder.Parent = ServerStorage
readFileToParent("./assets/ServerAssets.rbxmx", serverAssetsFolder)

--Remote signals
local remoteSignalFolder = Instance.new("Folder")
remoteSignalFolder.Name = "RemoteSignals"
remoteSignalFolder.Parent = ReplicatedStorage
readFileToParent("./assets/RemoteSignals.rbxmx", remoteSignalFolder)

--Serialise services into a game, deserialise that into a place, and then serialise that to a file.
local gameData =
	roblox.serializeModel({ Workspace, Lighting, ReplicatedStorage, ServerStorage, MaterialService, StarterGui })
local game = roblox.deserializePlace(gameData)

local file = roblox.serializePlace(game)
fs.writeFile("./Place.rbxlx", file)
'''

importAssets='''local roblox = require("@lune/roblox")
local fs = require("@lune/fs")

local content = fs.readFile("Place.rbxlx")
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
'''