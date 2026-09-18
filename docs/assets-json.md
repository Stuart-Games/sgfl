# `assets.json` reference

`assets.json` (project root, required) declares which parts of the place `sgfl save` writes into the repo and `sgfl start`/`publish` apply back. Anything not declared here — and not a script synced by Rojo — is not versioned.

```json
{
    "Materials":     { "folder": "assets", "robloxPath": "MaterialService" },
    "ServerSignals": { "folder": "assets", "robloxPath": "ServerStorage.Signals" },
    "Workspace":     { "folder": "map",    "robloxPath": "Workspace", "format": "blob" }
}
```

Each entry is `name → { folder, robloxPath }`:

- **`robloxPath`** — a whole service (`MaterialService`) or a dot-joined path inside one (`ServerStorage.Signals`).
- **`folder`** — where the entry's files are written. Entries may share a folder.
- **No overlap** — no two entries' `robloxPath` may be equal or one a prefix of the other (you can save `ServerStorage.Signals` or all of `ServerStorage`, not both), except via [delegation](#delegating-a-child-to-another-entry).

## Files an entry produces

| File | When | Contents |
|---|---|---|
| `{Entry}.sgfl` | always | Diffable text: properties, attributes, tags, references |
| `{Entry}.sgfl.rbxm` | blob-format entries | The subtree's children, serialized by the Roblox engine (binary — track with [Git LFS](../README.md#git-lfs)) |

Geometry-heavy entries (`Workspace`, `ReplicatedAssets`, `ServerAssets`, `ShipModules`) default to blob; everything else defaults to text. Override with `"format": "text"` or `"format": "blob"`.

Rules of thumb:

- Never hand-edit `[!sidecar .]` blocks in `.sgfl` files — they hold binary-exact engine state maintained by `sgfl save`.
- Only the exact `{folder}/{Entry}.sgfl(.rbxm)` paths are read or written. Other files in those folders (art sources, READMEs, leftovers from removed entries) are ignored and never deleted.
- A missing entry file on `start`/`publish` means that entry keeps whatever the rojo build produced. A declared path missing from the place on `save` warns loudly.

## Extended config (`$version: 2`)

A `robloxPath` deeper than two segments, `mode`, `include` and `exclude` all require `"$version": 2` at the top level:

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

- **`mode: "file"`** (default) — the whole subtree in one `{Entry}.sgfl` (+ `.sgfl.rbxm` for blob).
- **`mode: "children"`** — one file per direct child: `{ChildName}.sgfl` (text) or `{ChildName}.sgfl.rbxm` (blob), plus `{Entry}.sgfl` for the container's own properties. The `folder` becomes exclusive to this entry, and `sgfl save` deletes `.sgfl`/`.sgfl.rbxm` files in it that no longer match a child — keep unrelated files out. Child names must be filesystem-safe and unique case-insensitively, or `save` fails and asks you to rename in Studio.
- **`include` / `exclude`** — patterns matched against direct children only, include first, then exclude. A pattern is a name glob (`"Temp*"`) or `"$ClassName"` (matched with `IsA`). Children filtered out are **unmanaged**: `save` never writes them, `start`/`publish` never clear them, and a leftover file for one is ignored.

If a teammate on sgfl 2.0.x hits `TypeError: 'int' object is not subscriptable`, their sgfl predates `$version` — run `sgfl update`.

### Delegating a child to another entry

The one exception to the no-overlap rule: an entry may hand a direct child to a second entry by excluding that child's name.

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

`StarterGui.Shared` is now owned entirely by `SharedGui` (its own folder — which can live in a submodule — and its own mode); `StarterGui` never reads, writes or clears anything under it.

- The delegating exclude must be a **literal name glob** (`"Shared"`, `"Sh*"`). A `"$ClassName"` pattern is not accepted, because it can't be verified without a live instance.
- Each level of nesting needs its own exclude: if `SharedGui` delegates one of *its* children further down, `SharedGui` must exclude that child too.

## Post-apply hook

An optional `postapply.luau` in the project root — plain Luau, `return function(game) ... end` — runs inside the engine after the entry files are applied and before the place is saved.
