local process = require("@lune/process")
local serde = require("@lune/serde")
local fs = require("@lune/fs")

local shared = {}

export type TableType = {
	[string]: {
		folder: string,
		robloxPath: string,
	},
}

function shared.getRbxmxString(folder: string, fileName: string): string
	return `{folder}/{fileName}.rbxmx`
end

function shared.getSgflFile(relativePath: string): string
	return process.args[1] .. relativePath
end

function shared.getAssetTreeJson(): TableType
	local filePath = if fs.isFile("assets.json") then "assets.json" else shared.getSgflFile("json/default.assets.json")
	return serde.decode("json", fs.readFile(filePath))
end

return shared
