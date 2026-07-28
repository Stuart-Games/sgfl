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

`PLACE_ID_BUILD` is reserved. It designates an empty scratch place the cloud apply runs against, and it is never published to. Declare one: the apply uploads an asset-less base version to its target place before applying your entry files, so if the task fails, that target is left holding a build with no assets. Pointing it at a scratch place keeps that off your live places entirely.

`UNIVERSE_ID_BUILD` is optional and puts that scratch place in a universe of its own. Without it the build place must live in `UNIVERSE_ID` (execution tasks are universe-scoped). With it, a build resolves no publish targets at all — it needs only those two ids and the three keys, so a build job can run holding nothing that can reach a real game, and one build place can serve every repo in an org. See [Using sgfl across an organization](#using-sgfl-across-an-organization).

Values already present in the environment take precedence over `.env.<env>` (a warning names any that were shadowed), so CI can supply ids and keys as secrets without the committed file overriding them.

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
- **`include` / `exclude`** — lists of patterns matched against direct children of the entry root only (not deeper descendants), evaluated include-then-exclude. A pattern is either a name glob (`"Temp*"`, `*` matches anything) or `"$ClassName"` (matches via `IsA`). Children that don't match are **unmanaged**: `sgfl save` never writes them and `sgfl publish`/`sgfl start` never clears them — whatever is live in the place for that child survives untouched. A file left over from before a child was excluded is ignored on publish too, so excluding a child can't duplicate it.
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

run the command `pipx install git+https://github.com/Stuart-Games/sgfl.git`.

That tracks `main`. CI and anything that needs a reproducible build should pin to a release tag instead — `pipx install git+https://github.com/Stuart-Games/sgfl.git@v2.6.0`. Releases are cut automatically from the `version` field in `pyproject.toml` whenever it changes on `main`, so every version that exists has a tag.

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

Avoid running `sgfl start` or `sgfl publish` against the same place at the same time as a teammate: each run creates place versions while the cloud session works, and overlapping runs can each end up looking at the other's version. `sgfl` detects the common cases and aborts rather than publishing the wrong build, but the safe habit is one publisher at a time.

### To Save

run the command `sgfl save`.

This projects the current state of the place (latest version, saved or published — a Studio "Save" without publishing is picked up) back into the asset files, ready to commit.

Before anything is written, `sgfl` checks whether the projection would empty an entry that currently has content committed. If so it lists them and asks for an explicit YES/NO confirmation (defaulting to NO); a non-interactive session writes nothing at all. The usual cause is not that you cleared something in Studio, but that a `sgfl start`/`publish` died partway: those commands upload the rojo build (scripts only, no assets) to the place *before* applying your asset files, so a failed run leaves the place's latest version empty of assets — re-publish before saving. Files sgfl doesn't own (entries you removed, vendored libraries, other games in the same repo) are never touched: only the exact `{folder}/{Entry}.sgfl` paths your `assets.json` declares are read or written. Leftovers are inert; delete them yourself whenever you like.

`sgfl` refuses to run outside a project — if there's no `assets.json` in the current directory you get an error rather than a run against default settings.

### To Publish

run the command `sgfl publish <env>` (e.g. `sgfl publish prod`).

Builds once and uploads the same final place to every `PLACE_ID_<NAME>` in `.env.<env>`. Requires typed confirmation. Useful flags: `--dry-run` (runs the whole build and stops before uploading), `--places lobby,arena`, `--version-type Saved`, `--json report.json`.

### Automated publishing (CI/CD)

`sgfl publish` is the interactive one-shot. For anything unattended, use the two halves separately:

```bash
sgfl build prod --out dist/place.rbxl
```

```bash
sgfl upload dist/place.rbxl prod --expect-places main,lobby --json dist/report.json
```

`build` does the expensive, rate-limited half — rojo, the cloud apply, the sidecar patch — against the scratch build place and writes the finished bytes to disk. `upload` promotes those exact bytes and nothing else, so what you validated is what ships, a partial failure is safe to re-run, and promoting the same artifact to a second environment costs no execution task.

Set `SGFL_CI=1` for automated runs. That swaps the interactive confirmation for `--expect-places`: the upload aborts unless the env file resolves to exactly the places you named, so adding a `PLACE_ID_<NAME>` can't silently widen what a workflow publishes to. It also makes `PLACE_ID_BUILD` mandatory and lets a missing `.env.<env>` fall back to the process environment.

sgfl ships no workflow files. The pipeline lives once, as a reusable workflow in your org — see below — and each game repo carries only a short caller. It needs three secrets: `SGFL_PUBLISH_KEY`, `SGFL_DOWNLOAD_KEY`, `SGFL_EXECUTION_KEY`.

There is no rollback command. The build artifact is the rollback mechanism: keep the artifacts your workflow uploads, and `sgfl upload` a previous one to revert.

### Using sgfl across an organization

**Put the API keys on a dedicated service account, not a person's.** Roblox has no group-owned API keys — every key belongs to a user account and inherits that account's permissions. The supported pattern is a separate account invited to the group with a minimal role, owning `PUBLISH_KEY`, `DOWNLOAD_KEY` and `EXECUTION_KEY`. A developer's personal key takes the whole pipeline down when they leave or rotate it, publishes under their name, and carries their access to every other group resource.

**Key permissions stop at the experience.** Scopes are granted per experience (universe), never per place, so a key that can build can also publish to every live place in that experience. What you *can* split is capability: the build job needs Luau Execution + place publishing + asset delivery, while the upload job needs only place publishing, so issue the upload key with strictly less.

**Give the build its own universe.** One empty place, in an experience containing no game, pointed at by `UNIVERSE_ID_BUILD` + `PLACE_ID_BUILD`. Since scopes can't be narrowed below an experience, this is the only way to make a build credential that genuinely cannot reach a live place: put nothing reachable in its experience. It also makes the build the same everywhere — a build resolves no publish targets, so those two ids are the *only* project config a build job needs, and one build place serves every repo.

**Define it all once, at the org.** Organization → Settings → Secrets and variables → Actions. The three keys go in as **secrets** (`SGFL_PUBLISH_KEY` / `SGFL_DOWNLOAD_KEY` / `SGFL_EXECUTION_KEY`); the two build ids go in as **variables** (`UNIVERSE_ID_BUILD` / `PLACE_ID_BUILD`) since they aren't secret. Scope both sets to the game repos, not just the workflow repo — in a reusable workflow `vars` and `secrets` resolve against the repo that *triggered* the run. A game repo then needs nothing of its own to build. (Org secrets for private repos need a Team plan or higher.)

**Maintain the workflow once.** The pipeline lives in a dedicated org repo (for Stuart Games, `Stuart-Games/cicd`) as a `workflow_call` workflow that runs `sgfl build`, uploads the artifact, then runs `sgfl upload` in an environment-gated job. Tag it `v1`. Each game repo then carries only a caller:

```yaml
name: sgfl publish
on:
  workflow_dispatch:
    inputs:
      env:
        required: true
        default: production
      places:
        required: true
        default: main
jobs:
  publish:
    uses: Stuart-Games/cicd/.github/workflows/sgfl-publish.yml@v1
    with:
      env: ${{ inputs.env }}
      places: ${{ inputs.places }}
    secrets: inherit
```

Approvals stay per repo — GitHub environments can't be defined org-wide — so each game still needs its `production` environment with required reviewers under Settings → Environments.

**Keep the sgfl version in the reusable workflow, not the caller.** A per-repo `sgflRef` means upgrading the fleet is one PR per game, which is how repos quietly fall years behind. Put the version in the workflow's input default instead: one bump reaches everything, and the `v1` tag on the workflow repo is the lever if you need to stage a rollout. A workflow that only builds can float on `main` outright — it ships nothing to players, so a bad sgfl change surfaces as a red smoke run, which is the early warning you want.

**Nothing serializes a shared build place, so sgfl fails loud instead.** Many repos building against one place means nothing guarantees the version your apply produced is the next one — and GitHub can't fix that upstream, because `concurrency` groups are scoped to the triggering repository and a group defined in a reusable workflow won't span callers. So with `UNIVERSE_ID_BUILD` set, sgfl stops inferring: if the engine doesn't report the version it saved, the build fails rather than risk downloading a version another repo's build made. Those failures are re-runnable; publishing another game's place file would not be.

This only applies to `build`/`publish`. `sgfl start` applies against `PLACE_ID` — your own dev place — and is unaffected either way.

**The rate limit is per key owner, not per key.** Task creation is capped at 5/minute for whoever owns the key, so every game sharing the group's key draws on one budget, and issuing more keys under the same group buys nothing. One `sgfl build` is one task, so this only bites when several games build at once; sgfl backs off and retries on 429 rather than failing. The per-repo concurrency group serializes a single repo, not the org.

### To Create a New Project

run the command `sgfl init` in an empty directory and follow the prompts.

### Migrating a pre-2.0 Project

Projects saved with sgfl 1.x (per-entry `.rbxm` files) must be migrated once: run `sgfl migrate` and follow the confirmation prompt. It projects the current place into the new format and deletes the legacy entry files. Until then, `start`/`save`/`publish` will refuse to run (older sgfl versions keep working on unmigrated projects).

If the project used a Lune `postbuild.luau` hook, port it to `postapply.luau` — plain Luau exporting `return function(game)` — which runs inside the engine right before the place is saved.
