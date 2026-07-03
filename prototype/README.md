# Cloud text-projection prototype

Prototyping a replacement for the Lune/rbx-dom serialization path: an Open Cloud
**Luau Execution Session** runs a script *inside the engine* with the place loaded,
walks the DataModel, and emits a deterministic **text projection** of each
`assets.json` subtree for git. Writes go the other way: an imperative apply script +
`AssetService:SavePlaceAsync()`, then sgfl republishes the saved bytes untouched.

Why: rbx-dom-based tools (Lune, Rojo) lag Roblox binary-format changes and have
active writer bugs (empty `StyleRule.PropertiesSerialize` corruption, July 2026).
The engine's own serializer/reflection can't lag by definition. Design history:
whole-place-file versioning was rejected long ago (referent churn kills diffs/merges);
per-service *binary* blobs from the engine writer were rejected (no byte-stability
contract → no-op saves would dirty every file). A text projection we control gives
per-subtree isolation, no-op stability, and line-level reviewable diffs.

## Files

- `projection_probe.luau` — probe task answering the design's open questions:
  ReflectionService availability in sessions, the readable-vs-unreadable serialized
  property sets per service, SerializationService non-creatable rejection + repeat-byte
  stability, Terrain `CopyRegion` → `TerrainRegion` serialization, and a trial
  projection dump of small services.
- `run_task.py` — creates the task, polls, prints logs + JSON results.

## Running

1. Creator Hub → Open Cloud → API Keys → new key with the **Luau Execution Sessions**
   API system (read + write), scoped to the target universe.
2. `set SGFL_EXECUTION_KEY=<key>` (or `$env:SGFL_EXECUTION_KEY = "<key>"`)
3. `py run_task.py projection_probe.luau --env <path-to-project>/.env`

The probe is **read-only**: it never calls `SavePlaceAsync`, so it cannot affect the
place or its version history.

## Probe checklist (what "good" looks like)

- [ ] `reflection.available = true` — ReflectionService exists in sessions
- [ ] `propertyAccess.*.unreadable` lists are small and acceptable (these are exactly
      the properties the projection can't version — they persist in the live place)
- [ ] `serialization.serializeServiceResult` / `...StarterPlayerScriptsResult` are
      errors (confirms documented non-creatable behavior, incl. non-service containers)
- [ ] `serialization.repeatSerializeIdentical` — informs whether embedded binary blobs
      (terrain) are byte-stable across saves
- [ ] `terrain.serializedBytes` present and sane; `copyRegionSeconds` acceptable
- [ ] `projection.*` samples look right and sizes are reasonable
