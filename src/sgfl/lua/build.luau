local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local serde = require("@lune/serde")
local Instance = roblox.Instance

type TableType = {
	[string]: {
		folder: string,
		robloxPath: string,
	},
}

function readAsset(filePath: string)
	if fs.isFile(filePath) then
		local asset = roblox.deserializeModel(fs.readFile(filePath))[1]
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

local json: TableType = serde.decode("json", fs.readFile("assets.json"))

local serviceInstances = {}

--parses services and folders within those services
for fileName, data in json do
	local splitPath = data.robloxPath:split(".")
	local directService = #splitPath == 1
	local serviceName = splitPath[1]

	--somethign like workspace where its thought of as a singular object
	if directService then
		local service = readAsset(`{data.folder}/{fileName}.rbxmx`)
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
		readFileToParent(`{data.folder}/{fileName}.rbxmx`, folder)
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
fs.writeFile("./Place.rbxlx", file)
