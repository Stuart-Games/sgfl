local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local shared = require("./shared")

local Instance = roblox.Instance

function readAsset(filePath: string)
	if fs.isFile(filePath) then
		local modelFile = fs.readFile(filePath)
		local asset = roblox.deserializeModel(modelFile)[1]
		return asset
	end

	return nil
end

function readFileToParent(filePath: string, parent: Instance)
	local masterModel = readAsset(filePath)

	if masterModel then
		for i, child in masterModel:GetChildren() do
			child.Parent = parent
		end
	end
end

local serviceInstances = {}

--parses services and folders within those services
for fileName, data in shared.getAssetTreeJson() do
	local splitPath = data.robloxPath:split(".")
	local directService = #splitPath == 1
	local serviceName = splitPath[1]

	--somethign like workspace where its thought of as a singular object
	local rbxmFile = shared.getRbxmString(data.folder, fileName)

	if directService then
		local service = readAsset(rbxmFile)
		serviceInstances[serviceName] = service
	else --subfolders of services
		local service = serviceInstances[serviceName]
		if not service then
			serviceInstances[serviceName] = Instance.new(serviceName)
			service = serviceInstances[serviceName]
		end

		local folder = Instance.new("Folder")
		folder.Name = splitPath[2]
		folder.Parent = service
		readFileToParent(rbxmFile, folder)
	end
end

local instanceTable = {}

for _, instance in serviceInstances do
	table.insert(instanceTable, instance)
end

--Serialise services into a game, deserialise that into a place, and then serialise that to a file.
local gameData = roblox.serializeModel(instanceTable)
local game = roblox.deserializePlace(gameData)
local file = roblox.serializePlace(game)
fs.writeFile("./Place.rbxl", file)
