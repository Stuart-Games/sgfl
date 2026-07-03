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

NB: There is support for direct service read/write and subfolder read/write but not both for the same service. In the example above you can see we save a specific folder in ServerStorage but all of MaterialService. We would not, in this case, be able to save all of ServerStorage to one file lest we have duplication in the file system.

Each entry produces text files in its folder:

- `{Entry}.sgfl` — a deterministic, diffable text projection of the subtree (properties, attributes, tags, references).
- `{Entry}.sgfl.rbxm` — only for blob-format entries: the subtree's children serialized by the engine. Geometry-heavy entries (`Workspace`, asset containers) default to blob; everything else defaults to text. Override per entry with `"format": "text"` or `"format": "blob"`.

Do not hand-edit `[!sidecar .]` blocks inside `.sgfl` files — they carry binary-exact engine state and are maintained by `sgfl save`. Other files in the asset folders (art sources, READMEs) are never touched. `Place.rbxl` is a temporary build artifact and must not be committed.

## Installation

run the command `pipx install git+https://github.com/devvf/sgfl.git`.

## Upgrading

run the command `sgfl update` (or `pipx upgrade sgfl`) to update to the newest version if available.

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

### To Publish

run the command `sgfl publish <env>` (e.g. `sgfl publish prod`).

Builds once and uploads the same final place to every `PLACE_ID_<NAME>` in `.env.<env>`. Requires typed confirmation. Useful flags: `--dry-run`, `--places lobby,arena`, `--version-type Saved`.

### To Create a New Project

run the command `sgfl init` in an empty directory and follow the prompts.

### Migrating a pre-2.0 Project

Projects saved with sgfl 1.x (per-entry `.rbxm` files) must be migrated once: run `sgfl migrate` and follow the confirmation prompt. It projects the current place into the new format and deletes the legacy entry files. Until then, `start`/`save`/`publish` will refuse to run (older sgfl versions keep working on unmigrated projects).

If the project used a Lune `postbuild.luau` hook, port it to `postapply.luau` — plain Luau exporting `return function(game)` — which runs inside the engine right before the place is saved.
