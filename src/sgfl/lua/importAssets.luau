local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local serde = require("@lune/serde")
local process = require("@lune/process")

type TableType = {
	[string]: {
		folder: string,
		robloxPath: string,
	},
}

local content = fs.readFile("Place.rbxlx")
local game = roblox.deserializePlace(content)

--Destroy camera to avoid merge conflict as it is regenerated anyway
local camera = game.Workspace:FindFirstChild("Camera") :: Camera?
if camera then
	camera:Destroy()
end

local filePath = if fs.isFile("assets.json") then "assets.json" else process.args[1]
local json: TableType = serde.decode("json", fs.readFile(filePath))

for fileName, data in json do
	if not fs.isDir(data.folder) then
		fs.writeDir(data.folder)
	end

	local splitPath = data.robloxPath:split(".")
	local modelPath = if #splitPath == 1 then game[splitPath[1]] else game[splitPath[1]][splitPath[2]]
	local model = roblox.serializeModel({ modelPath }, true)

	fs.writeFile(`{data.folder}/{fileName}.rbxmx`, model)
end
