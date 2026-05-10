local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local shared = require("./shared")

local Instance = roblox.Instance

local function readAsset(filePath: string)
	if fs.isFile(filePath) then
		local modelFile = fs.readFile(filePath)
		local asset = roblox.deserializeModel(modelFile)[1]
		return asset
	end

	return nil
end

local function clearChildren(target: Instance)
	for _, child in target:GetChildren() do
		child:Destroy()
	end
end

local function moveChildrenInto(source: Instance, target: Instance)
	for _, child in source:GetChildren() do
		child.Parent = target
	end
end

--Place.rbxl is produced by `rojo build` before this script runs, so the
--DataModel already contains every script tree declared in default.project.json.
--We layer the binary rbxm assets on top of that, replacing only the services
--and folders explicitly listed in assets.json.
local placeBytes = fs.readFile("Place.rbxl")
local game = roblox.deserializePlace(placeBytes)

for fileName, data in shared.getAssetTreeJson() do
	local rbxmFile = shared.getRbxmString(data.folder, fileName)
	local rbxmRoot = readAsset(rbxmFile)

	if not rbxmRoot then
		continue
	end

	local splitPath = data.robloxPath:split(".")
	local serviceName = splitPath[1]

	local service = game:FindFirstChild(serviceName)
	if not service then
		service = Instance.new(serviceName)
		service.Parent = game
	end

	if #splitPath == 1 then
		clearChildren(service)
		moveChildrenInto(rbxmRoot, service)
	else
		local folderName = splitPath[2]
		local folder = service:FindFirstChild(folderName)
		if folder then
			clearChildren(folder)
		else
			folder = Instance.new("Folder")
			folder.Name = folderName
			folder.Parent = service
		end
		moveChildrenInto(rbxmRoot, folder)
	end
end

--Optional user hook: postbuild.luau in the project root may `return` a
--`function(game)` that mutates the in-memory DataModel before serialization.
if fs.isFile("postbuild.luau") then
	local loaded, syntaxErr = loadstring(fs.readFile("postbuild.luau"), "postbuild.luau")
	if not loaded then
		error("postbuild.luau failed to parse: " .. tostring(syntaxErr))
	end
	local hook = loaded()
	if type(hook) == "function" then
		hook(game)
	else
		error("postbuild.luau must `return function(game) ... end` (got " .. type(hook) .. ")")
	end
end

local file = roblox.serializePlace(game)
fs.writeFile("./Place.rbxl", file)
