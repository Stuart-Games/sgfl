# Publishing and CI

Day-to-day development only needs `sgfl start` and `sgfl save` (see the [README](../README.md)). This page covers shipping a build to real places, by hand or from CI.

## Environments

An `.env.<env>` file (e.g. `.env.prod`) describes one publish environment. It holds only ids, so it is safe to commit:

```
UNIVERSE_ID=1234567890
PLACE_ID_MAIN=111
PLACE_ID_LOBBY=222
PLACE_ID_BUILD=999
```

- **`PLACE_ID_<NAME>`** — one per place to publish to.
- **`PLACE_ID_BUILD`** — reserved: an empty scratch place the build runs against. It is never published to. Declare one: the build uploads a scripts-only base to its target before applying your assets, so a build that dies leaves that place holding an asset-less version. A scratch place keeps that off your live places.
- **`UNIVERSE_ID_BUILD`** — optional: puts the scratch place in a universe of its own (see [Across an organization](#across-an-organization)).

Values already set in the real environment win over the file (a warning names each one), so CI can supply ids and keys as secrets.

## Publishing by hand

```bash
sgfl publish prod
```

Builds once and uploads the same final place to every `PLACE_ID_<NAME>` in `.env.prod`. Requires typed confirmation at a terminal.

| Flag | Effect |
|---|---|
| `--dry-run` | Run the whole build, stop before uploading |
| `--places lobby,arena` | Publish to a subset |
| `--version-type Saved` | Save instead of publish |
| `--no-build` | Reuse an existing `Place.rbxl` |
| `--json report.json` | Write per-place version numbers |
| `--fail-on-warn` | Treat integrity warnings as failures |

Only one person should `start`/`publish` against a given place at a time. sgfl detects the common collisions and aborts rather than ship the wrong build, but overlapping runs waste everyone's time.

## Publishing from CI

Use the two halves separately:

```bash
sgfl preflight prod
```

```bash
sgfl build prod --out dist/place.rbxl --fail-on-warn
```

```bash
sgfl upload dist/place.rbxl prod --expect-places main,lobby --json dist/report.json
```

- **`preflight`** checks every key is present and alive, and does nothing else. Run it first: an expired key otherwise surfaces minutes into a build as `User unauthorized to update place`, naming neither the key nor the expiry.
- **`build`** does the expensive, rate-limited half (rojo, cloud apply, sidecar patch) against the build place and writes the finished bytes to disk. It publishes nothing.
- **`upload`** sends those exact bytes to each target. What you validated is what ships; a partial failure is safe to re-run; promoting the same artifact to another environment costs nothing.
- **`--fail-on-warn`** matters unattended. sgfl warns and continues when part of a build didn't make it in (a blob that didn't deserialize, a sidecar property it couldn't write). A checkout missing its Git LFS objects does exactly this — every blob entry becomes a pointer stub, and the run goes green having dropped most of the game.

Set **`SGFL_CI=1`** for automated runs. It replaces the interactive confirmation with `--expect-places` (the upload aborts unless the env file resolves to exactly the places named, so adding a `PLACE_ID_<NAME>` can't silently widen a workflow), makes `PLACE_ID_BUILD` mandatory, and lets a missing `.env.<env>` fall back to the process environment.

CI needs three secrets: `SGFL_PUBLISH_KEY`, `SGFL_DOWNLOAD_KEY`, `SGFL_EXECUTION_KEY`. Pin sgfl to a release tag for reproducible builds:

```bash
pipx install git+https://github.com/Stuart-Games/sgfl.git@v2.11.0
```

**There is no rollback command.** The build artifact is the rollback: keep the artifacts your workflow uploads and `sgfl upload` an older one to revert.

## Across an organization

sgfl ships no workflow files. The pipeline lives once, as a reusable workflow in an org repo, and each game repo carries only a short caller.

- **Use a service account for the keys.** Roblox has no group-owned API keys; every key belongs to a user and inherits their permissions. Invite a dedicated account to the group with a minimal role and have it own all three keys. A personal key breaks the pipeline when that person leaves.
- **Split keys by capability.** Permissions are granted per experience, never per place, so a key that can build can also publish anywhere in that experience. The build job needs Luau execution + place publishing + asset delivery; the upload job needs only place publishing — give it a lesser key.
- **Give the build its own universe.** One empty place in an experience containing no game, pointed at by `UNIVERSE_ID_BUILD` + `PLACE_ID_BUILD`. This is the only way to make build credentials that cannot reach a live place. A build then needs only those two ids and the three keys, and one build place serves every repo.
- **Grant `DOWNLOAD_KEY` the Assets read permission on the build universe.** Nothing tells sgfl which version its apply saved, so without this it assumes `base + 1`. With it, sgfl reads the versions that actually exist.
- **A shared build place fails loud.** With `UNIVERSE_ID_BUILD` set, sgfl refuses to guess: if it can't identify the version it saved (another repo built at the same moment), the build fails rather than risk shipping someone else's place. Re-run it. GitHub can't prevent the overlap — `concurrency` groups don't span repositories. `sgfl start` is unaffected; it runs against your own dev place.
- **The rate limit is per key owner.** Task creation is capped at 5/minute for the account owning the key, shared by every game using it. One build is one task; sgfl backs off and retries on 429.
- **Define secrets once, at the org.** Organization → Settings → Secrets and variables → Actions. The three keys are **secrets**; `UNIVERSE_ID_BUILD` / `PLACE_ID_BUILD` are **variables**. Scope both to the game repos — in a reusable workflow, `vars` and `secrets` resolve against the repo that triggered the run. (Org secrets for private repos need a Team plan.)
- **Keep the sgfl version in the reusable workflow, not the callers**, so one bump upgrades every game.

The reusable workflow lives in `Stuart-Games/cicd` as a `workflow_call` workflow (build → artifact → environment-gated upload). A game repo's caller:

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

Approvals stay per repo: each game needs its own `production` environment with required reviewers (Settings → Environments).
