# sgfl project conventions

Shared rules for every Roblox project built with sgfl and Rojo. Each repo's own `CLAUDE.md` adds project facts and may override anything here.

sgfl writes this file into each project as `.claude/rules/sgfl-conventions.md` (gitignored) and refreshes it on every command run there, so edit the copy in the sgfl repo (`src/sgfl/misc/claude-conventions.md`), never the one in a project.

## What an sgfl project is

- **Code lives on disk, instances live in Studio.** Rojo maps `src/` and the submodules into the place and live-syncs script changes while the developer works. Everything that is not code (models, remotes, signals, attributes, GUI, lighting) lives in the Studio place and is projected into `.sgfl` text files by `sgfl save`. `assets.json` lists the projected entries; `default.project.json` is the Rojo tree.
- **`.sgfl` files are a snapshot, not a live view.** They update only when the developer runs `sgfl save`, which pulls from the Roblox place. Read them for structure, but inspect the live place through the Studio MCP server when it matters.
- **Never edit `.sgfl` files.** They are overwritten by the next save and the edit never reaches the place. Change the live place via the Studio MCP server (or ask the developer), who then saves and runs `sgfl save`. If a file edit is genuinely needed, ask first.
- `.sgfl.rbxm` blobs are binary (Git LFS pointers in some repos). Never hand-edit them or the `[!sidecar .]` blocks.
- The sgfl projection drops `WorldPivot`. A model whose pivot matters needs a `PrimaryPart` (with `PivotOffset` if the pivot is not the part), or the pivot silently becomes the bounding-box centre on the next build.
- You do not have access to Roblox assets: no `.rbxm` contents, no Signals/Remote layouts inside Roblox containers. Ask instead of trawling.

## Toolchain

- Rokit manages `rojo`, `wally`, `lune` and `wally-package-types` (`~/.rokit/bin`). **Do not run `rojo`, `wally`, `sgfl` or other toolchain commands.** The developer runs them; Rojo is already serving, so script edits sync instantly and need no reminder.
- No `luau-lsp`, `selene` or `stylua` is installed. The only offline syntax check is Lune: a script that reads each file and `pcall(luau.compile, source)`, run with `lune run <script> <files...>`. Lune 0.10.5 predates `const` and explicit generic calls (`Set.new<<T>>()`), so gsub `\nconst ` to `\nlocal ` and `<<...>>(` to `(` first, or the check stops at that line.
- **Never check out a different commit in a submodule, or switch superproject branches, while Rojo is serving.** The file churn syncs into the place and a mid-burst disconnect leaves a mix of old and new scripts; this has broken live games. Move branch pointers without touching files (`git branch -f main <commit>`), and only `git checkout` when the worktree is already at that commit.
- Rojo's watcher can miss `sed -i` (it replaces the file). Edit with the Edit tool or a Python open/write, and after any suspicious edit confirm the synced `Source` in Studio before testing.
- Put edit scripts on disk rather than piping them through a Bash heredoc, which unescapes backslashes once and turns `"\r\n"` into control characters.
- **`Edit` "String to replace not found"** is almost always a tab mismatch: sources indent with hard tabs. Start `old_string` mid-line for single-line changes, or copy the exact bytes from a fresh narrow `Read`. As a last resort do the replacement in PowerShell with `[System.IO.File]::ReadAllText`/`WriteAllText` and `New-Object System.Text.UTF8Encoding($false)`, building tabs with `$t = "\`t"`. Files are UTF-8 without BOM, LF line endings.
- Don't prefix Bash with `cd "path" && ...`; the working directory is already set and the compound form defeats prefix-based allowlists.
- `gh` is installed (on Windows at `C:\Program Files\GitHub CLI\gh.exe` if it is not on PATH). **Always confirm before creating, editing or deleting issues.**
- **Roblox docs:** `create.roblox.com/docs/...` pages are JS-only. Fetch the Markdown source instead: `https://raw.githubusercontent.com/Roblox/creator-docs/main/content/en-us/<same-path>.md`.

## Studio MCP server

- Call `get_studio_state` first. Make MCP edits only in Edit mode; in Play mode, tell the developer and wait, and treat reads as possibly stale since they may be mid-game.
- `execute_luau` runs in the Assistant sandbox (identity 10, reduced capabilities). In Edit mode it **cannot `require` project modules or reparent scripts**, and even where a require works it gets a fresh module table with empty state. Use it for inspecting and editing non-script instances, and for deleting scratch scripts.
- To run project code: write a tagged `Script` with `multi_edit` (for example `ServerScriptService.MCPCheck`), `start_stop_play(true)`, `get_console_output`, `start_stop_play(false)`, then `Destroy()` it via `execute_luau`. Each Play starts fresh, which also side-steps the stale require cache (Rojo updates `Source` in place, so a cached module survives the sync). If the first `multi_edit` is refused, say what you want to create and why, and retry once the developer says go. Never leave scratch scripts in the place.
- An MCP edit that only changes attributes may not mark the place modified, so Studio skips the save and `sgfl save` silently omits it. After such an edit, ask the developer to make a trivial manual edit before saving, and diff the saved `.sgfl` for the expected lines before committing.
- Running project code in Edit mode can write derived attributes onto live templates, which the next `sgfl save` captures as diff noise. Say so when it is likely, and prefer the Server datamodel under Play for tests.
- If the Roblox tools show "Connected, 0 tools", the proxy's `tools/list` is missing `ttlMs`/`cacheScope` and Claude Code rejects it. The fix is the stdio shim at `~/.claude/mcp-shims/roblox_studio_legacy_shim.py` in the project's MCP entry; check with `claude mcp get Roblox_Studio`.

## Working with the developer

- **Ask questions when in doubt** rather than guessing.
- There are no automated tests; Studio is the only test surface. When debugging and stuck, add tagged `print` diagnostics for the developer to run rather than guessing further, and ask for more information when you cannot find the issue. Drive repros through instances and remotes, not module state.
- The developer builds models and visual assets by hand in Studio. Supply the functional contract, the code and the checks; do not try to build visuals.
- Flag performance costs before adding them: per-frame allocations, new `RunService` connections, hot-path table churn. Prefer extending an existing per-frame loop over starting a new one.

## Live games and data

- Projects have separate dev, testing and live universes (see each project's place-id module). Before any DataStore write or publish, check which universe the open place belongs to; places in different universes often share a name.
- **Never write to a testing or live DataStore without the developer's explicit go-ahead.** Those hold real player data. The dev universe is the place for test writes.
- Do not break saves. Any change to a serialized format, DataStore key or schema must keep existing data loading, via a reader for the old version or a migration. Flag before changing.
- Flag balance-affecting tweaks (physics constants, NPC behaviour, gamepass values, rates) so the developer can decide.
- Roblox UserIds have had 11 digits since 2025-11-22, and a developer's own id is usually shorter. Size any UserId-prefixed DataStore key with an 11-digit id, keep it at most 50 characters, and test with a long fake id.

## Submodules

`sglib` (the shared library, including the react-lua UI core) and any other shared submodules are git submodules used by several projects. A change must be committed in the submodule and the gitlink bumped in **every** superproject. Never copy code between two clones of a submodule; use git, and if git cannot be used, ask. UI work follows `sglib/UICore/React/DESIGN.md`; read it before any UI change.

## Roblox and sglib gotchas

- `Vector3:FuzzyEq` and `CFrame:FuzzyEq` are relative: the epsilon scales with magnitude, so at a few hundred studs a 0.05 tolerance accepts whole studs. Use absolute comparisons for positions and bounds; FuzzyEq is fine only for unit vectors and rotations.
- sglib `Set` defines no `__len`, so `#someSet` is always 0. Use `.Count`, `:contains`, and `==` (it defines `__eq`).
- "Script timeout: exhausted allowed execution time" keeps re-raising at the next interrupt after a `pcall` catches it, until the thread yields. Run long work on a separate thread and poll it, so the caller's bookkeeping always runs.
- Scripts under `StarterCharacterScripts` re-run on every respawn, so captured references reset by design.
- Inputs use the **Input Action System** (instance based). Consult the Roblox docs if unsure.

## Code style

Luau, strict mode (`.luaurc`). All new code carries type annotations.

- No arbitrary semicolons.
- Prefer `.ChildName` over `:FindFirstChild("ChildName")`: remotes, signals and asset children referenced in code are always properly parented. If the developer said something exists, trust that it exists.
- Don't type-cast when the type is already known.
- `CONSTANT_VALUES` go at the top of the file, declared with `const`. Prefer named constants over inline magic numbers.
- Loop indexes are `i` when appropriate, even if unused.
- Reference <https://luau.org> for idiomatic Luau.
- Avoid em dashes; they cause formatting issues.
- **Avoid creating new modules** if the content could reasonably live in an existing one. If in doubt, ask before adding one.
- **No lazy requires.** All requires sit at the top of the file.
- Always require relative to the server/client `init` scripts (see existing patterns). Never require via `StarterPlayerScripts` or `StarterPlayer` directly.

**Comments** explain *why*, not *what*. Default to fewer and shorter; when unsure whether a comment is needed, delete it. These are hard limits:

- Module headers: one short paragraph (about 6 lines). State what the module is and the one or two non-obvious contracts. Never narrate mechanics or derive maths.
- Docstrings: a one-line summary plus `@param`/`@return`, nothing more. Add a `why` line only if it is non-obvious and documented nowhere else.
- Inline comments: one line, only where the code is not self-evident. Never restate the adjacent line.
- Never put multi-line comments on constants; avoid them outside the top of a function.
- Do not repeat the same boilerplate across sibling modules.
- When modifying a file, re-check any comment changes against these rules.

**Docstrings** go on every function except trivial or self-explanatory ones:

```lua
--[[
    Function that turns Foos into Bars

    @param foo The Foo to turn into a Bar

    @return A new Bar
]]
```

**File ordering** (deviate only with a good reason). Types precede constants because a constant may be of one of those types.

1. Services (`game:GetService(...)`)
2. Requires
3. Type aliases from required files (e.g. `type Set<T> = Set.Set<T>`)
4. Module declaration
5. Types
6. Global constants
7. Global variables
8. Constructor(s), if applicable
9. Functions
10. Module return
