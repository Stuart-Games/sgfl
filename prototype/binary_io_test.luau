--[[
	binary_io_test.luau — sanity test for the driver's binary output support and
	version-pinned task creation. Read-only. Returns a known byte pattern as
	BinaryOutput plus the place version as a JSON return value; optionally echoes
	the size of a BinaryInput if one was attached.
]]

local taskInput = ({ ... })[1]

local payload = "sgfl-binary-output-test:" .. string.rep("\0\1\2\3\255", 20)
local buf = buffer.fromstring(payload)

local binaryInputSize = nil
if taskInput and taskInput.BinaryInput then
	binaryInputSize = buffer.len(taskInput.BinaryInput)
end

return {
	BinaryOutput = buf,
	ReturnValues = {
		{
			placeVersion = game.PlaceVersion,
			placeName = game.Name,
			payloadBytes = buffer.len(buf),
			binaryInputSize = binaryInputSize,
		},
	},
}
