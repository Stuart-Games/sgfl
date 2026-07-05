# sgfl — Stuart Games Roblox Workflow Tool

A Python CLI that orchestrates the Roblox development loop for Stuart Games projects. Since v2.0 the serialization layer is the **cloud projection pipeline**: Luau tasks running inside real Roblox engine instances via [Open Cloud Luau Execution Sessions](https://create.roblox.com/docs/cloud/reference/features/luau-execution) project each `assets.json` entry into git-friendly files and apply them back. **No rbx-dom anywhere** (no Lune, no Rojo serialization) — the engine that owns the binary format does all reading/writing, so Roblox format changes cannot break the pipeline. [Rojo](https://rojo.space/) remains for what it is good at: file↔Studio script sync and `rojo build`. End users install via `pipx` and invoke `sgfl <command>`.

## High-level Flow

Top-level subcommands — `start`, `save`, `init`, `publish`, `migrate`, `update` — plus an `auth` subcommand group (`auth login` / `auth status`). Each is a real Typer subcommand registered in [cli.py](src/sgfl/cli.py).

- **`sgfl start [-p]`** — full publish+launch loop.
  1. Optional `git pull` (`-p` / `--pull`).
  2. `rojo build` → `Place.rbxl` (scripts + project tree only).
  3. **Cloud apply** (`cloud.buildFinalPlace`): upload `Place.rbxl` as a **Saved** version → version-pinned Luau execution task applies every entry file inside the engine (runs `postapply.luau` if present) → `SavePlaceAsync` → download the saved bytes → **sidecar patch** (script-unreachable properties written directly into the binary) → final bytes.
  4. POST the final bytes to `versions?versionType=Published` (same v1 endpoint as always).
  5. Open Roblox Studio via `roblox-studio:` URI (Windows/macOS; Linux intentionally unsupported), open VS Code (`code .`), then `rojo serve` (blocks).
- **`sgfl save`** — project the current place state back into the repo. One Luau execution task walks the `assets.json` subtrees and returns all entry files in a single binary output; the driver then downloads the exact place version the task saw, extracts the binary sidecar (with an **anchor canary**: script-readable values from the session must match the binary decode bit-exactly, else nothing is written), and writes the files. Note: the task and the unversioned asset-delivery route both operate on the **latest version, saved or published** — a Studio "Save" without publish will be picked up. After writing, `sweepStaleEntryFiles` (also run at the start of `start`/`publish`) scans the project for `.sgfl`/`.sgfl.rbxm` files no current entry accounts for (entry removed / folder moved) and offers to delete them behind an explicit YES toggle — the expected-file set is **config-derived, never write-derived**, so an entry that failed to project keeps its committed files; non-TTY sessions list and keep. The per-save children-folder sweep (`_sweepChildrenFolders`) likewise skips any entry whose root file wasn't produced this save.
- **`sgfl migrate`** — one-time conversion of a legacy (`.rbxm`) project. Lists the legacy entry files, requires typing `MIGRATE` at a TTY (refuses non-interactive), runs a cloud save, deletes the legacy per-entry `.rbxm`/`.rbxmx` files (exact entry names only — never other files in shared folders), and prints a `postbuild.luau` → `postapply.luau` porting reminder if needed. `start`/`save`/`publish` refuse to run on unmigrated projects and point here. Old sgfl versions keep working on unmigrated projects.
- **`sgfl init`** — scaffold a new project (same layout as before: `src/{Shared,...}` with `Shared` wrapped under ReplicatedStorage, `assets.json`, `default.project.json`, `.env` with prompted `PLACE_ID`/`UNIVERSE_ID`, `.gitignore`, `.gitattributes` (`*.sgfl text eol=lf` — see CRLF guard below), `rokit init/add rojo/install` — **no Lune**). No seed asset files are written: entry files appear on the first `sgfl save`, and a publish with a missing entry file simply keeps the base content for that entry.
- **`sgfl publish <env>`** — multi-place publish. Loads `.env.<env>`, discovers `PLACE_ID_<NAME>` entries, `rojo build` once, runs the cloud apply once (Saved versions are created on the `main` place if declared, else the first sorted name), then uploads the same final bytes to each place. Flags: `--dry-run`, `--no-build` (reuse existing `Place.rbxl`), `--places lobby,arena`, `--version-type Published|Saved`, `-d`. Confirmation guard unchanged: typed `PUBLISH <env>` + arrow-key toggle, refuses non-TTY, no `--yes` by design.
- **`sgfl auth login` / `auth status`** — manage `~/.sgfl/credentials`: `PUBLISH_KEY`, `DOWNLOAD_KEY`, **`EXECUTION_KEY`**, `USER_ID`. Same dual-mode behavior as before (interactive masked prompts, or partial non-interactive updates via `--publish-key`/`--download-key`/`--execution-key`/`--user-id`).

`Place.rbxl` is still an **ephemeral build artifact** (rojo output, uploaded as the Saved base, then deleted — including on failure via `finally`). It must NOT be committed.

**Post-apply hook**: optional `postapply.luau` in the project root — plain Luau, `return function(game)`, inlined into the apply task and run inside the engine before `SavePlaceAsync`. This replaces the old Lune-based `postbuild.luau` (no longer executed). StyleLink→StyleSheet re-linking hacks are obsolete: cross-entry references are preserved natively.

## The Entry File Format

Each `assets.json` entry produces:

- **Text tier** (default): `{folder}/{EntryName}.sgfl` — a deterministic text projection. One block per instance in canonical order (`[path/with:occurrence] ClassName` headers, sorted `Prop = value` lines with non-default elision, `@Attr.*`, `@Tags`, `@Style.*` for StyleRules via `GetProperties`, heredocs for multi-line strings — including `Script.Source` since v2.1.0, f32-shortest float formatting, `Ref(./…)` / `Ref(game/…)` reference paths). Subtrees whose essence lives in serialized-but-script-unreachable properties (MeshPart, UnionOperation, MaterialVariant, SurfaceAppearance, …) are auto-detected via the reflection sweep and embedded as base64 **blob islands** (`!blob` blocks).
- **Blob tier** (`Workspace`, `ReplicatedAssets`, `ServerAssets`, `ShipModules` by default; override per entry with `"format": "text"|"blob"` in `assets.json`): `{folder}/{EntryName}.sgfl` (the container's own properties) + `{folder}/{EntryName}.sgfl.rbxm` (the children, serialized by the engine via `SerializationService`). Engine blobs are byte-deterministic across sessions and across publish→save cycles.
- **`[!sidecar .]` blocks** (appended to service-level entries' `.sgfl` by the driver): properties that cannot cross the Luau boundary — NotScriptable streaming settings, `Workspace.CollisionGroupData`, `Lighting.Technology`, service-root `AttributesSerialize` blobs (which carry load-bearing CoreScript-gated `RBX_*` migration attributes), plus whatever hidden root props the projection reports (self-extending). On publish these are patched byte-exact into the final binary by [sidecar.py](src/sgfl/sidecar.py). Never hand-edit sidecar lines except via `sgfl save`.

Invariants the pipeline guarantees (preserve them when changing anything): **no-op stability** (save twice → zero diffs), **round-trip identity** (publish → save → zero diffs), **per-entry isolation** (a change in one subtree never dirties another entry's files), and **fail-loud** (unknown value types, canary drift, unresolved refs, unpatchable sidecar props all abort rather than silently lose data).

Details that took real debugging — do not regress them: **the reflection sweep passes ALL SecurityCapabilities** (v2.1.0, `REFLECTION_OPTS` in projection.luau) — the default `GetPropertiesOfClass` hides capability-gated serialized props *entirely*, which silently projected scripts as empty shells (`Script.Source`) and would drop SurfaceAppearance/PBR maps in text tier; with the full sweep, gated props classify as readable (text), hidden (island / dynamic sidecar), or curated-benign (`BENIGN_HIDDEN`/`BENIGN_HIDDEN_PREFIXES` — Studio editor state like `GameSettings*`, `ChatTranslation*`, localization autoscrape, `LevelOfDetail`), so new engine props self-report instead of vanishing; non-Archivable instances are engine-injected session content and are skipped; `Workspace.Camera` is stripped; Rojo-mounted subtrees (parsed from `default.project.json`) are excluded from projection and never touched by apply (fixes the old stale-script hazard on `ReplicatedFirst`/`StarterPlayer`); parent-locked singleton children (TextChatService configs, Terrain, StarterPlayerScripts) cannot be reordered at apply, so projection emits them in canonical order (non-creatables first, sorted); props readable in sessions but not writable (`StreamingEnabled`, `LightingStyle`, `ChatVersion`, …) are routed back from the apply task for binary patching; `RBX_*` attributes are excluded from text; StyleRule `PropertyTransitionsSerialize` is FFlag-gated and probed on every save so it fails loudly the day Roblox ships transitions; **CRLF guard**: a git checkout with `core.autocrlf=true` (Git for Windows default) rewrites `.sgfl` files to CRLF, which broke every structural line and let apply clear subtrees without rebuilding them — now `collectEntryFiles` rejects CRLF entry files before any cloud call, apply.luau errors on any structural line ending in `\r` (heredoc bodies are tab-prefixed and may legitimately end in raw `\r`, so only unindented lines count), apply parses **before** clearing, and `init` writes a `.gitattributes` ([misc/gitattributes.txt](src/sgfl/misc/gitattributes.txt)).

## Auth & Env Layering

Unchanged three-source layering, later wins: `~/.sgfl/credentials` (always, silent if missing) → `.env` (no env arg) or `.env.<env>` (env arg, `override=True`, hard-fail if missing). `.env.<env>` files contain only IDs and are safe to commit; the generated `.gitignore` ignores `.env` but not `.env.*`.

`EXECUTION_KEY` also has a silent legacy fallback: `~/.sgfl/execution.key` (a bare key file) is read if the env var is unset ([cloud.py](src/sgfl/cloud.py) `getExecutionKey`).

## Codebase Layout

All Python under [src/sgfl/](src/sgfl/) (hatchling `src/` layout, entry point `sgfl = "sgfl.cli:app"`).

- [cli.py](src/sgfl/cli.py) — Typer wiring (six top-level commands + nested `auth`).
- [sgfl.py](src/sgfl/sgfl.py) — one Typer entry function per subcommand; shared `_runTask` error plumbing and `_loadEnv` layered loading, exactly as before. `migrate` accepts the same optional positional `<env>` as `save`.
- [operations.py](src/sgfl/operations.py) — task implementations (`startPlace`, `savePlace`, `migratePlace`, `initPlace`, `publishPlaces`, `authLogin`, `authStatus`) plus the HTTP-diagnostic helpers and per-place upload worker. The legacy guard is `_checkForLegacyAssets`/`_legacyEntryFiles`.
- [cloud.py](src/sgfl/cloud.py) — the pipeline core: execution-task create/poll/logs (version-pinned, binary input/output), asset-delivery download, the manifest+data container format shared with the Luau scripts, `buildProjectionConfig` (entries + tiers + Rojo-mounted paths + sidecar/anchor prop lists), `runProjectionSave` (save direction incl. sidecar merge + canary), `buildFinalPlace` (publish direction incl. patch-back), `uploadPlaceVersion`. **Rate limit: task creation is 5/min per API key owner** — the pipeline uses exactly one task per save and one per publish.
- [sidecar.py](src/sgfl/sidecar.py) — binary `.rbxl` chunk parser: `extract` (allowlist → values) and `patchFile` (values → uncompressed chunk re-emission). Only the chunk container and six primitive type encodings are parsed; everything else is opaque bytes. `ALLOWLIST`/`ANCHORS` are the static prop sets; the dynamic part comes from the projection task at runtime. Compressed chunks are LZ4 (hand-rolled, no dependency) or zstd (`zstdDecompress`: stdlib `compression.zstd` on Python 3.14+, else the `zstandard` package — the one real third-party runtime dependency in the project).
- [lua/cloud/projection.luau](src/sgfl/lua/cloud/projection.luau) / [lua/cloud/apply.luau](src/sgfl/lua/cloud/apply.luau) — the in-engine scripts. The driver replaces `__SGFL_CONFIG__` (JSON) and, for apply, `__SGFL_POSTAPPLY__`. They must stay symmetric: every value encoder in projection needs a decoder in apply.
- [json/default.assets.json](src/sgfl/json/default.assets.json), [json/default.project.json](src/sgfl/json/default.project.json), [misc/gitignore.txt](src/sgfl/misc/gitignore.txt) — init templates.
- The prototyping history (probes, standalone drivers, validation evidence) that produced this pipeline was removed after the v2.0 cutover; it remains in git history under `prototype/` on the `prototype/cloud-projection` branch.

## Environment Variables

| Key | Lives in (recommended) | Used by | Notes |
|---|---|---|---|
| `PUBLISH_KEY` | `~/.sgfl/credentials` | start, publish | Open Cloud key with place-publish scope |
| `DOWNLOAD_KEY` | `~/.sgfl/credentials` | start, save, publish, migrate | asset-delivery scope (sidecar download) |
| `EXECUTION_KEY` | `~/.sgfl/credentials` | start, save, publish, migrate | **Luau Execution Sessions** system, read + write, scoped to the universe |
| `USER_ID` | `~/.sgfl/credentials` | start | numeric user id for the `roblox-studio:` URI |
| `PLACE_ID` | `.env` or `.env.<env>` | start, save, migrate | numeric place id |
| `UNIVERSE_ID` | `.env` or `.env.<env>` | all cloud commands | execution tasks are universe-scoped, so `save` now needs it too |
| `PLACE_ID_<NAME>` | `.env.<env>` | publish | one entry per publish target |

The deprecated `--env-suffix` / `-e` flag on `start`/`save` still works; prefer the positional `<env>` arg.

## `assets.json` Semantics

Entry name → `{folder, robloxPath}` plus the optional `"format": "text" | "blob"` key. `robloxPath` is service-level (one segment) or a dot-joined path of arbitrary depth (`Service.Folder`, `Service.Folder.SubFolder`, …) resolved segment-by-segment in both Luau scripts. No two entries' `robloxPath` may overlap (equal, or one a prefix of the other) at any depth — unless the shorter (ancestor) entry excludes the next path segment via a literal name glob, delegating that subtree to the other entry (a `"$ClassName"` exclude doesn't count: it can't be statically verified). Multiple entries may still share a `folder`. Entry files are written/read strictly by entry name (or, in `mode: "children"`, by child name within the entry's dedicated folder) — arbitrary other files in asset folders (art sources, READMEs) are never touched. Missing entry file on publish = that entry keeps the rojo-built base content; missing declared path on save = loud warning.

**`normalizeAssetConfig`** in [cloud.py](src/sgfl/cloud.py) is the single source of truth for interpreting `assets.json` — called once by `operations._loadAssetTable`, so every consumer (legacy check, start/save/migrate/publish, `buildProjectionConfig`) sees fully-normalized entries (`format`/`mode`/`include`/`exclude`/`pathSegments` always populated) and never sees `$`-prefixed keys. It enforces:

- **`$version`**: required (and must be `2`) iff any entry uses `mode`, `include`, `exclude`, or a `robloxPath` deeper than 2 segments. A `$version` newer than `SUPPORTED_ASSET_VERSION` fails with a "run `sgfl update`" error; an unrecognized `$version` key on pre-2.1 sgfl fails closed with a `TypeError` before any cloud call or file write (verified, not a hazard to guard against further).
- **`mode: "file"`** (default, unchanged) vs **`mode: "children"`** — the entry's `folder` becomes exclusive (checked across all entries) and produces `{Entry}.sgfl` (container root's own props/attrs/tags only) plus one file per managed direct child: `{ChildName}.sgfl` (text tier) or `{ChildName}.sgfl.rbxm` (blob tier, whole child as one engine blob). `sgfl save` deletes stale `.sgfl`/`.sgfl.rbxm` files in the folder that no longer correspond to a managed child.
- **`include`/`exclude`**: lists of name globs (`"Temp*"`) or `"$ClassName"` (`IsA` match), applied only to direct children of the entry root (both modes), include-then-exclude precedence. Filtered-out children are unmanaged: `sgfl save` never writes them, `sgfl start`/`publish` never clears them.
- Child names in `mode: "children"` must be filesystem-safe and case-insensitively unique, or `sgfl save` fails loud (Studio rename required) rather than colliding or dropping data.

The Luau scripts duplicate a small `isManaged`/glob-matching helper (no `require` between the two standalone task scripts) — keep both copies identical, same as the value encoder/decoder tables.

## Error Handling Pattern

Unchanged: everything raises `SGFLError(message, details, suggestions, ...)`, rendered once by `printSgflError` via `_runTask`. `cloud.py` has its own `_cloudRequest` wrapper producing SGFLErrors with HTTP diagnostics and the standard 401/403 hint; for new HTTP calls in operations.py keep using `_httpDiagnostics`/`_withAuthorizationWarning`; for external commands use `runCommand`.

## Conventions

- **camelCase for Python identifiers** — match the existing style; do not Pythonify to snake_case.
- **`announceStep("...")`** before each user-visible phase; green `SUCCESS` line on completion; loud yellow `WARN` lines, never silent drops.
- `pytest` unit tests live in [tests/](tests/) — pure-function and file-I/O coverage for `sidecar.py` (synthetic `.rbxl` chunks built in [tests/helpers.py](tests/helpers.py)) and the `cloud.py`/`util.py` helpers that don't require live Roblox cloud calls (`normalizeAssetConfig`, `buildProjectionConfig`, `collectEntryFiles`, `parseSidecarBlocks`, container pack/unpack, env/version helpers). No coverage for the actual HTTP/execution-task calls — those need a real universe. Run with `pip install -e ".[test]"` then `pytest`. [.github/workflows/tests.yml](.github/workflows/tests.yml) runs the same on push to `main` and on every PR (Python 3.9 + 3.13 matrix). No lint config. Distribution via `pipx install git+https://github.com/devvf/sgfl.git`.
- Bump `version` in [pyproject.toml](pyproject.toml) for user-visible changes (v2.0.0 = the cloud-pipeline cutover; v2.1.0 = extensible `assets.json` — `mode`/`include`/`exclude`/`$version`).
- StyLua targets latest release.

## Cross-Platform Notes

- `start` opens Studio on Windows and macOS; **Linux is silently a no-op** for that step. VS Code launch uses `shell=True` (Windows `.cmd` shim).
- The pipeline itself is fully cross-platform (stdlib + requests only; no platform subprocesses beyond rojo/git/code).
- All Roblox HTTP calls use explicit timeouts (30s API, 300s transfers in `cloud.py`).

## When Modifying This Project

- Touching the entry file format, the `isManaged`/glob-matching filter helper, or `resolveEntryRoot`'s path-walk? Update **both** [lua/cloud/projection.luau](src/sgfl/lua/cloud/projection.luau) (encode) and [lua/cloud/apply.luau](src/sgfl/lua/cloud/apply.luau) (decode) — they must stay symmetric — and re-verify the two gates on a real place: save twice (no-op stability) and publish→save (round-trip identity).
- Adding a sidecar property? Usually unnecessary since v2.1.0: the full-capability reflection sweep sees every serialized prop, so new hidden service-root props flow into the dynamic sidecar automatically (typeId discovered from the binary; undecodable types WARN). Add a static `(class, prop): typeId` to `ALLOWLIST` in [sidecar.py](src/sgfl/sidecar.py) only to pin an expected typeId (and `ANCHORS` if script-readable). To *exclude* a junk Studio-state prop instead, add it to `BENIGN_HIDDEN` in projection.luau.
- Adding a new env var? Add it to `API_ENV_KEYS`/`ID_ENV_KEYS` in [util.py](src/sgfl/util.py), to `envDiagnosticKeys` for the relevant subcommands in [sgfl.py](src/sgfl/sgfl.py), and — if per-developer — to `CREDENTIAL_KEYS` plus the `auth login` prompts/flags.
- Adding a new subcommand? Wire it in four places: [sgfl.py](src/sgfl/sgfl.py) (Typer function using `_runTask`), [cli.py](src/sgfl/cli.py) (`app.command()`), [operations.py](src/sgfl/operations.py) (implementation), `_toolDiagnosticsForTask` in [util.py](src/sgfl/util.py).
- Adding a publish flag? [sgfl.py](src/sgfl/sgfl.py) option → forward in the `_runTask` lambda → consume in `publishPlaces`.
- Don't introduce a hard dependency on Linux Studio launching, and don't reintroduce rbx-dom-based tooling into the save/publish path — surviving Roblox format changes is the whole point of v2.
