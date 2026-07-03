--[[
	write_probe.luau — FIRST WRITE PROBE. Validates the unproven leg of the
	pipeline: can an execution session persist changes via SavePlaceAsync?

	Creates a saved version only — the published/live place is never touched.

	1. Record game.PlaceVersion as seen by the session.
	2. Create a marker Folder in ServerStorage with identifying attributes.
	3. AssetService:SavePlaceAsync().
	4. Report PlaceVersion before/after (does the session see the new number?).
]]

local AssetService = game:GetService("AssetService")
local ServerStorage = game:GetService("ServerStorage")

local out = {}
out.placeVersionBefore = game.PlaceVersion

-- clean any marker from a previous probe run first, so reruns stay tidy
for _, child in ServerStorage:GetChildren() do
	if child.Name == "SGFL_WriteProbe" then
		child:Destroy()
		out.removedStaleMarker = true
	end
end

local marker = Instance.new("Folder")
marker.Name = "SGFL_WriteProbe"
marker:SetAttribute("Probe", "sgfl-cloud-pipeline write_probe")
marker:SetAttribute("BaseVersion", game.PlaceVersion)
marker.Parent = ServerStorage

local okSave, saveErr = pcall(function()
	AssetService:SavePlaceAsync()
end)
out.savePlaceAsync = okSave and "OK" or tostring(saveErr)
out.placeVersionAfter = game.PlaceVersion

-- does a second save in the same session also work? (repeatability signal)
if okSave then
	marker:SetAttribute("SecondSave", true)
	local okSave2, saveErr2 = pcall(function()
		AssetService:SavePlaceAsync()
	end)
	out.secondSave = okSave2 and "OK" or tostring(saveErr2)
	out.placeVersionAfterSecond = game.PlaceVersion
end

return out
