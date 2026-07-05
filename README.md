# Stuart Games Workflow Package (sgfl)

## Purpose

This package enables the building and saving of Stuart Games Roblox projects in a consistent and portable manner.

Since v2.0, saving and publishing run through the **cloud projection pipeline**: a Luau task executed inside a real Roblox engine instance (via Open Cloud Luau Execution Sessions) reads and writes the place, so sgfl never parses or emits Roblox's binary format itself and cannot be broken by format changes. [Rojo](https://rojo.space/) is still used for script sync and building the base place.

## Requirements

1. Python 3.9 or greater
2. A [rojo](https://rojo.space/) installation, either globally or in the project directory (`sgfl init` sets one up via rokit)
3. Credentials and project env keys, listed below

### Credentials (per developer, stored in `~/.sgfl/credentials`)

Set these once with `sgfl auth login` (check them with `sgfl auth status`):

```
USER_ID        your user id with edit perms
PUBLISH_KEY    Open Cloud key with place-publish scope
DOWNLOAD_KEY   Open Cloud key with asset-delivery (download) scope
EXECUTION_KEY  Open Cloud key for Luau Execution Sessions (read + write), scoped to the universe
```

### Project keys (in the project's `.env`)

```
PLACE_ID       the id of the place we are working on
UNIVERSE_ID    the id of the experience it belongs to
```

For multi-place publishing, an `.env.<env>` file (e.g. `.env.prod`) holds one `PLACE_ID_<NAME>` per target place plus `UNIVERSE_ID`. These files contain only ids and are safe to commit.

## `assets.json`

An `assets.json` file is required in the root directory to specify which parts of the place are saved.

Example structure:

```json
{
    "Materials": {
        "folder": "assets",
        "robloxPath": "MaterialService"
    },
    "ServerSignals": {
        "folder": "assets",
        "robloxPath": "ServerStorage.Signals"
    },
    "Workspace": {
        "folder": "map",
        "robloxPath": "Workspace",
        "format": "blob"
    }
}
```

NB: There is support for direct service read/write and subfolder read/write but not both for the same service. In the example above you can see we save a specific folder in ServerStorage but all of MaterialService. We would not, in this case, be able to save all of ServerStorage to one file lest we have duplication in the file system. More generally, no two entries' `robloxPath` may overlap (one a prefix of the other) at any depth — unless the shorter entry delegates the subtree away (see "Delegating a nested subtree" below).

Each entry produces text files in its folder:

- `{Entry}.sgfl` — a deterministic, diffable text projection of the subtree (properties, attributes, tags, references).
- `{Entry}.sgfl.rbxm` — only for blob-format entries: the subtree's children serialized by the engine. Geometry-heavy entries (`Workspace`, asset containers) default to blob; everything else defaults to text. Override per entry with `"format": "text"` or `"format": "blob"`.

Do not hand-edit `[!sidecar .]` blocks inside `.sgfl` files — they carry binary-exact engine state and are maintained by `sgfl save`. Other files in the asset folders (art sources, READMEs) are never touched. `Place.rbxl` is a temporary build artifact and must not be committed.

### Extended config (`$version: 2`)

Entries also support a `robloxPath` deeper than two segments (e.g. `"ReplicatedStorage.Assets.NPCs"`), a `mode`, and `include`/`exclude` filters. Using any of these requires declaring `"$version": 2` at the top level of `assets.json`:

```json
{
    "$version": 2,
    "NPCs": {
        "folder": "assets/npcs",
        "robloxPath": "ReplicatedStorage.Assets.NPCs",
        "mode": "children",
        "exclude": ["Temp*", "$Script"]
    }
}
```

- **`mode: "file"`** (default) — the whole subtree in one `{Entry}.sgfl` (+ `.sgfl.rbxm` for blob tier), as above.
- **`mode: "children"`** — `folder` becomes dedicated to this entry (no other entry may share it). Produces `{Entry}.sgfl` holding only the container's own properties/attributes/tags, plus one file per managed direct child: `{ChildName}.sgfl` for text tier, `{ChildName}.sgfl.rbxm` (the whole child as one engine blob) for blob tier. `sgfl save` deletes stale `.sgfl`/`.sgfl.rbxm` files in the folder that no longer correspond to a managed child — don't put unrelated files there.
- **`include` / `exclude`** — lists of patterns matched against direct children of the entry root only (not deeper descendants), evaluated include-then-exclude. A pattern is either a name glob (`"Temp*"`, `*` matches anything) or `"$ClassName"` (matches via `IsA`). Children that don't match are **unmanaged**: `sgfl save` never writes them and `sgfl publish`/`sgfl start` never clears them — whatever is live in the place for that child survives untouched.
- Child names (in `mode: "children"`) must be filesystem-safe and unique case-insensitively; `sgfl save` errors out (renaming needed in Studio) rather than silently colliding or dropping data.

#### Delegating a nested subtree to another entry

Normally two entries can never have overlapping `robloxPath`s — not even one a prefix of the other. The one exception: an entry may hand a specific direct child off to a second, independent entry rooted at that child, by excluding the child's name from the first entry:

```json
{
    "$version": 2,
    "StarterGui": {
        "folder": "gui",
        "robloxPath": "StarterGui",
        "mode": "children",
        "exclude": ["Shared"]
    },
    "SharedGui": {
        "folder": "sglib/Gui",
        "robloxPath": "StarterGui.Shared",
        "mode": "children"
    }
}
```

Here every other top-level `StarterGui` child still auto-splits into `gui/<Name>.sgfl`, while `StarterGui.Shared` is entirely owned by `SharedGui` — its own dedicated folder (which can live in a different repo/submodule, same as `ShipModules` does), its own mode, its own per-child files. `StarterGui` never writes, reads, or clears anything under `Shared`; `sgfl` treats it exactly like any other excluded, unmanaged child.

The delegating exclude must be a **literal name glob** (`"Shared"`, `"Sh*"`, ...) — a `"$ClassName"` pattern isn't accepted for this because it can't be checked without a live instance, so it can't back the guarantee that the parent entry will truly leave that child alone. If you nest three or more levels deep, each direct link needs its own matching exclude (the top entry excluding `"Shared"` doesn't automatically cover `"Shared"`'s own child being delegated again further down — the entry that owns `StarterGui.Shared` would need to exclude that grandchild's name too).

## Installation

run the command `pipx install git+https://github.com/devvf/sgfl.git`.

## Upgrading

run the command `sgfl update` (or `pipx upgrade sgfl`) to update to the newest version if available.

If you pull a project whose `assets.json` declares `"$version": 2` (see the extended config section above) while running sgfl 2.0.x, you'll hit `TypeError: 'int' object is not subscriptable` before any cloud call or file write — that's the old parser fail-closing on an unrecognized key. Run `sgfl update`. A `$version` newer than any installed sgfl understands fails with a clear "run `sgfl update`" error instead.

## Commands

A full list of commands can be found by running `sgfl --help`.

To include detailed `.env`, API key, HTTP request, and runtime diagnostics when errors occur, add `-d` before any command, e.g. `sgfl -d save`.

### To Start

run the command `sgfl start`.

`-p` can be added after start to pull files from the relevant repository first.

This builds the place with rojo, applies the saved asset files inside a cloud engine session, publishes the result, then opens Roblox Studio and VS Code and runs `rojo serve` for live script sync.

### To Save

run the command `sgfl save`.

This projects the current state of the place (latest version, saved or published — a Studio "Save" without publishing is picked up) back into the asset files, ready to commit.

After writing, `sgfl` also scans the project for `.sgfl`/`.sgfl.rbxm` files that no current `assets.json` entry accounts for — leftovers from an entry you removed, or one whose `folder` you moved. It lists them and asks whether to delete, with an explicit YES/NO toggle (defaulting to NO, so you can't wipe files by accident); non-interactive sessions just list them and keep everything. The same check runs at the start of `sgfl start` and `sgfl publish`.

### To Publish

run the command `sgfl publish <env>` (e.g. `sgfl publish prod`).

Builds once and uploads the same final place to every `PLACE_ID_<NAME>` in `.env.<env>`. Requires typed confirmation. Useful flags: `--dry-run`, `--places lobby,arena`, `--version-type Saved`.

### To Create a New Project

run the command `sgfl init` in an empty directory and follow the prompts.

### Migrating a pre-2.0 Project

Projects saved with sgfl 1.x (per-entry `.rbxm` files) must be migrated once: run `sgfl migrate` and follow the confirmation prompt. It projects the current place into the new format and deletes the legacy entry files. Until then, `start`/`save`/`publish` will refuse to run (older sgfl versions keep working on unmigrated projects).

If the project used a Lune `postbuild.luau` hook, port it to `postapply.luau` — plain Luau exporting `return function(game)` — which runs inside the engine right before the place is saved.
