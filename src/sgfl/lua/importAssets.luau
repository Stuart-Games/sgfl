local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local shared = require("./shared")

local content = fs.readFile("Place.rbxlx")
local game = roblox.deserializePlace(content)

--Destroy camera to avoid merge conflict as it is regenerated anyway
local camera = game.Workspace:FindFirstChild("Camera") :: Camera?
if camera then
	camera:Destroy()
end

for fileName, data in shared.getAssetTreeJson() do
	if not fs.isDir(data.folder) then
		fs.writeDir(data.folder)
	end

	local splitPath = data.robloxPath:split(".")
	local modelPath = if #splitPath == 1 then game[splitPath[1]] else game[splitPath[1]][splitPath[2]]
	local model = roblox.serializeModel({ modelPath }, true)

	fs.writeFile(`{data.folder}/{fileName}.rbxmx`, model)
end
