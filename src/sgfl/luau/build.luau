local roblox = require("@lune/roblox") :: any
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
fs.writeFile("./WaterPhysics.rbxlx", file)
