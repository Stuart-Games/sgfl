--[[
	patch_verify.luau — runs against the sidecar-patched Saved version (v57).
	If this task runs at all, the engine parsed our re-emitted uncompressed
	chunks. Verifies the readable patched props took effect, confirms collision
	groups loaded from the patched CollisionGroupData blob, then SavePlaceAsync
	so the NotScriptable patched values can be checked in the engine-rewritten
	file (proving they were internalized, not just tolerated).
]]

local AssetService = game:GetService("AssetService")
local PhysicsService = game:GetService("PhysicsService")

local out = {}
out.placeVersion = game.PlaceVersion
out.streamingEnabled = workspace.StreamingEnabled -- patched false -> true
out.fallenPartsDestroyHeight = workspace.FallenPartsDestroyHeight -- patched -500 -> -650

local groups = {}
for _, group in PhysicsService:GetRegisteredCollisionGroups() do
	table.insert(groups, group.name)
end
table.sort(groups)
out.collisionGroups = groups

local okSave, saveErr = pcall(function()
	AssetService:SavePlaceAsync()
end)
out.savePlaceAsync = okSave and "OK" or tostring(saveErr)

return out
