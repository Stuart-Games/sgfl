# sgfl — Stuart Games Roblox Workflow Tool

A Python CLI that orchestrates the Rojo/Lune-based Roblox development loop for Stuart Games projects. It glues together: the Roblox Open Cloud API (publish/download), [Lune](https://lune-org.github.io/docs) (running Luau scripts headless), and [Rojo](https://rojo.space/) (file ↔ Studio sync). End users install via `pipx` and invoke `sgfl <command>`.

## High-level Flow

There are four top-level subcommands — `start`, `save`, `init`, `publish` — plus an `auth` subcommand group with `auth login` / `auth status`. Each is a real Typer subcommand with its own argument signature (registered in [cli.py](src/sgfl/cli.py)).

- **`sgfl start [-p]`** — full publish+launch loop.
  1. Optional `git pull` (`-p` / `--pull`).
  2. Run [lua/build.luau](src/sgfl/lua/build.luau) via Lune. Reads each `*.rbxm` referenced by `assets.json`, assembles them into a `DataModel`, writes `Place.rbxl`.
  3. POST the binary `Place.rbxl` to `https://apis.roblox.com/universes/v1/{UNIVERSE_ID}/places/{PLACE_ID}/versions?versionType=Published` with `x-api-key: PUBLISH_KEY`.
  4. Open Roblox Studio via `roblox-studio:` URI scheme (Windows: `cmd /c start`; macOS: `open`). Linux is intentionally not handled.
  5. Delete the local `Place.rbxl`, open VS Code (`code .`), then `rojo serve` (blocks).
- **`sgfl save`** — pull current published place state back into the repo.
  1. GET `https://apis.roblox.com/asset-delivery-api/v1/assetId/{PLACE_ID}` with `x-api-key: DOWNLOAD_KEY` to get a signed `location` URL.
  2. Download that URL → `Place.rbxl`.
  3. Run [lua/importAssets.luau](src/sgfl/lua/importAssets.luau) via Lune to deserialise the place and write `*.rbxm` files into the folders defined by `assets.json`.
  4. Delete legacy `*.rbxmx` (XML) siblings in those folders.
- **`sgfl init`** — scaffold a new project (creates `src/{Shared,Shared/Util,ServerScriptService,ReplicatedFirst,StarterPlayerScripts,StarterCharacterScripts}` (shared code lives in `src/Shared`, mounted at `ReplicatedStorage.Shared` — deliberately wrapped in a `Shared` folder rather than mounted loose at the top of ReplicatedStorage, which causes replication issues; the client-side player-script locations `StarterPlayerScripts`/`StarterCharacterScripts` are mounted under the `StarterPlayer` service so code written there syncs out of the box; `ReplicatedFirst` is wrapped the same way as `Shared` — code mounts at `ReplicatedFirst.Shared` rather than the whole service, so non-script assets and the whole-service `ReplicatedFirst` `assets.json` entry don't collide with the Rojo-synced scripts), `assets.json`, `default.project.json`, `README.md`, `.env`, `.gitignore`, runs `rokit init/add lune/add rojo/install`). On a TTY it first prompts for the **project name** (defaults to the current directory name; written into `default.project.json`'s `name`) and the numeric **`PLACE_ID`** / **`UNIVERSE_ID`** (re-prompts on non-numeric input, Enter leaves blank) — these are the only two keys written to `.env`. Per-developer secrets (`PUBLISH_KEY`/`DOWNLOAD_KEY`/`USER_ID`) are deliberately **not** written to `.env`; if `~/.sgfl/credentials` is missing, init prints a `NEXT` reminder to run `sgfl auth login`. In non-interactive sessions (`sys.stdin.isatty()` false) prompting is skipped and `.env` IDs are written blank with the project name defaulting to the directory name, so headless scaffolding still works. Prompt helpers: `_promptInitConfig` / `_promptInitId` in [operations.py](src/sgfl/operations.py).
- **`sgfl publish <env>`** — multi-place publish. Loads `~/.sgfl/credentials` (see Auth & Env Layering below), then layers `.env.<env>` on top with `override=True`. Discovers every `PLACE_ID_<NAME>` entry from the merged env, builds `Place.rbxl` once via [lua/build.luau](src/sgfl/lua/build.luau), then uploads the same binary to each declared place sequentially via the same Open Cloud `/universes/{u}/places/{p}/versions` endpoint as `start`. Pure upload — does not open Studio, Rojo, or VS Code. Flags: `--dry-run`, `--no-build`, `--places lobby,arena` (subset filter), `--version-type Published|Saved`, `-d/--detailed`. Confirmation guard: prints a summary and refuses to proceed unless the user types exactly `PUBLISH <env>` at a TTY prompt; refuses outright in non-interactive sessions (`sys.stdin.isatty()` false). No `--yes` escape hatch by design — the whole point is preventing automation accidents.
- **`sgfl auth login`** — write/update `~/.sgfl/credentials` with the per-developer keys (`PUBLISH_KEY`, `DOWNLOAD_KEY`, `USER_ID`). Interactive by default (uses `getpass` to mask key entry, shows masked existing values, lets the user press Enter to keep each one). Non-interactive when any of `--publish-key`/`--download-key`/`--user-id` is passed — in that mode only the provided keys are updated; the rest are preserved. Refuses interactive mode in non-TTY sessions. Always writes the file with `0600` perms (no-op on Windows but harmless).
- **`sgfl auth status`** — print the credentials file path, mode, and a per-key audit (masked secret values, length, whitespace/numeric notes). Tells the user to run `auth login` if the file is missing.

`Place.rbxl` is treated as an **ephemeral build artifact** — it is created, used, then deleted on every `start`/`save`/`publish`. It must NOT be committed (the generated `.gitignore` excludes it).

**Post-build hook**: if a file named `postbuild.luau` exists in the project root, `start` and `publish` run it via `lune run postbuild.luau` after `lua/build.luau` finishes and before the upload (`save` doesn't run it — it's a build-time hook, not a download-time one). The hook can read/modify/rewrite `Place.rbxl` to patch around tooling bugs (e.g. Lune Roblox-instance quirks). The file is optional — if absent, the step is silently skipped. Implementation: `_runPostBuildHook` in [operations.py](src/sgfl/operations.py); path constant `POST_BUILD_HOOK_PATH` in [util.py](src/sgfl/util.py).

## Auth & Env Layering

Identity (per-developer secrets) is separate from project config. Three sources, layered with later wins on conflict:

1. **`~/.sgfl/credentials`** — per-developer identity. Holds `PUBLISH_KEY`, `DOWNLOAD_KEY`, `USER_ID`. Managed by `sgfl auth login` / `auth status`. `.env` format, `0600` perms. **Always loaded first** by every command (silent if missing).
2. **`<project>/.env`** — project-default config. Loaded only when no env arg is given. Holds `PLACE_ID`, `UNIVERSE_ID` and (legacy) any keys. Silent if missing.
3. **`<project>/.env.<env>`** — per-environment config. Loaded with `override=True` when an env arg is given. Holds `UNIVERSE_ID`, `PLACE_ID`, and (for publish) any number of `PLACE_ID_<NAME>` entries. Hard-fail if missing.

Selection rules:
- `sgfl start` / `sgfl save` → credentials + `.env`
- `sgfl start <env>` / `sgfl save <env>` → credentials + `.env.<env>` (`.env` is **not** loaded)
- `sgfl publish <env>` → credentials + `.env.<env>` (`<env>` is required)
- `sgfl auth login` / `auth status` → credentials only

This means a project's `.env.<env>` files are safe to commit — they only contain IDs, not secrets. The `.gitignore` template (`misc/gitignore.txt`) deliberately ignores `.env` but **not** `.env.*`, so per-env files end up in version control by default.

`getEnvSafe(key)` in [util.py](src/sgfl/util.py) returns context-aware "missing variable" hints: for keys in `CREDENTIAL_KEYS` (`PUBLISH_KEY`/`DOWNLOAD_KEY`/`USER_ID`) it points the user at `sgfl auth login`; for any other key it points at the `.env` / `.env.<env>` file.

The legacy **`--env-suffix` / `-e`** flag on `start`/`save` still works but is deprecated — the positional `<env>` arg replaces it. Passing both at once is an error.

## Codebase Layout

All Python lives under [src/sgfl/](src/sgfl/) (hatchling `src/` layout, entry point `sgfl = "sgfl.cli:app"` in [pyproject.toml](pyproject.toml)).

- [cli.py](src/sgfl/cli.py) — Typer app wiring. Registers four top-level subcommands (`start`, `save`, `init`, `publish`) via `app.command()` plus a nested `auth` Typer (`auth login`, `auth status`) via `app.add_typer(authApp, name="auth")`.
- [sgfl.py](src/sgfl/sgfl.py) — one Typer entry function per subcommand. Shared `_runTask(taskName, *, detailed, pullEnabled, envDiagnosticKeys, fn)` helper wraps every command in the same `SGFLError → printSgflError → typer.Exit(1)` plumbing. Shared `_loadEnv(env, envSuffix)` helper does the layered load: `loadCredentials()` first, then either `loadBaseEnv()` (no env arg) or `loadEnvFile(env)` (env arg given). `start`/`save` accept an optional positional `<env>` arg and a deprecated `--env-suffix` flag; passing both is rejected via `typer.BadParameter`.
- [operations.py](src/sgfl/operations.py) — task implementations (`startPlace`, `savePlace`, `initPlace`, `publishPlaces`, `authLogin`, `authStatus`) plus HTTP-diagnostic helpers (`_httpDiagnostics`, `_responseReason`, `_withAuthorizationWarning`) and the per-place worker `_publishPlaceFile`. `publishPlaces` aggregates per-place success/failure into a single summary table and raises one `SGFLError` with all per-place HTTP diagnostics if any place fails — no early exits mid-loop. `authLogin` is dual-mode: interactive (TTY-only, masked input via `getpass`) when no flags are passed; non-interactive (partial-update writes) when any of `--publish-key`/`--download-key`/`--user-id` is passed.
- [util.py](src/sgfl/util.py) — `SGFLError`, `runCommand`, `runLuauFile`, env-resolution (`getEnvSafe` with credential-vs-config-aware suggestions, `setEnvSuffix`), env-loading helpers (`loadCredentials()` for `~/.sgfl/credentials`, `loadBaseEnv()` for project `.env`, `loadEnvFile(env)` for `.env.<env>`, `saveCredentials(values)` for `auth login`), publish-flow helpers (`discoverPlaceIds()`, `confirmPublish(env, summaryLines)`), pretty-printing (`color`, `announceStep`, `printSgflError`), path helpers (`getFileURI` for cwd-relative, `getAbsoluteFileURI` for package-relative), constants `PLACE_FILE_PATH`, `ASSET_CONFIG_FILE_PATH`, `CREDENTIALS_DIR`, `CREDENTIALS_PATH`, `CREDENTIAL_KEYS`.
- [lua/](src/sgfl/lua/) — Luau scripts run via `lune run`. They `require("@lune/roblox" | "@lune/fs" | "@lune/serde" | "@lune/process")`. `process.args[1]` is the absolute path of the package's lua/ root, passed by `runLuauFile` so scripts can locate `json/default.assets.json` as a fallback.
- [json/default.assets.json](src/sgfl/json/default.assets.json) — default asset config copied to a project's `assets.json` on init.
- [json/default.project.json](src/sgfl/json/default.project.json) — default Rojo project file.
- [misc/gitignore.txt](src/sgfl/misc/gitignore.txt) — template `.gitignore` written on init.
- [__main__.py](src/sgfl/__main__.py) — supports `python -m sgfl`.

## Environment Variables

See "Auth & Env Layering" above for which file each command reads. Required keys per command:

| Key | Lives in (recommended) | Used by | Notes |
|---|---|---|---|
| `PUBLISH_KEY` | `~/.sgfl/credentials` | start, publish | Open Cloud API key with publish scope |
| `DOWNLOAD_KEY` | `~/.sgfl/credentials` | save | Open Cloud API key with asset-delivery scope |
| `USER_ID` | `~/.sgfl/credentials` | start | numeric user id used in `roblox-studio:` URI |
| `PLACE_ID` | `.env` or `.env.<env>` | start, save | numeric place id. Prompted for (with `UNIVERSE_ID`) by `sgfl init` and written to `.env` |
| `UNIVERSE_ID` | `.env` or `.env.<env>` | start, publish | numeric experience id. Prompted for by `sgfl init` |
| `PLACE_ID_<NAME>` | `.env.<env>` | publish | one entry per place to publish to (e.g. `PLACE_ID_LOBBY=111111`). `<name>` becomes the display name in summaries / `--places` filter |

Identity vs. config split: the first three are per-developer and rarely change, so they live in `~/.sgfl/credentials`. The bottom three are per-environment and safe to commit. Both are merged into `os.environ` at command start, so any project file can override the credentials file if it really needs to (e.g. project-pinned bot key).

**`--env-suffix` / `-e <SUFFIX>`** (start/save, deprecated) lets a single `.env` hold multiple environments by suffixing keys. With `-e MERGE`, lookups try `PUBLISH_KEY_MERGE` first and fall back to `PUBLISH_KEY` with a yellow `WARN`. Implementation lives in `setEnvSuffix` + `getEnvSafe` in [util.py](src/sgfl/util.py). Prefer the positional `<env>` arg with `.env.<env>` files. Passing both at once is a `typer.BadParameter`.

`-d` / `--detailed` enables expanded diagnostics on failure: env-var presence/length/placeholder-detection, tool resolution via `shutil.which`, HTTP status/headers/body preview, command stdout/stderr. Secrets are masked via `maskSecret` (first 4 + last 4 chars).

## `assets.json` Semantics

This file describes how the place tree is split into rbxm files. Each entry maps a logical name → `{folder, robloxPath}`. `robloxPath` is dot-separated and supports two shapes only:

- **Service-level** (one segment, e.g. `"MaterialService"`) — saves/loads the entire service.
- **Service.Folder** (two segments, e.g. `"ServerStorage.Signals"`) — saves/loads a specific folder under that service.

You **cannot mix** both forms for the same service (e.g. all of `ServerStorage` AND `ServerStorage.Signals`) — that would duplicate state on the filesystem. Multiple entries can share the same `folder` (e.g. several `assets/`). Deeper paths are not supported.

Files are stored as **binary `.rbxm`**. The save flow deletes any leftover `.rbxmx` (XML) — the project migrated away from XML in commit `46cbf41`. `_checkForOutdatedAssets` in [operations.py](src/sgfl/operations.py) refuses to publish if `.rbxmx` files are still around.

## Error Handling Pattern

All recoverable failures raise `SGFLError(message, details, suggestions, command, stdout, stderr, diagnostics)`. The CLI catches it once at the top of `start()` and renders via `printSgflError`. When extending the codebase:

- Wrap `requests.*` calls in `try/except requests.RequestException` and produce an `SGFLError` with HTTP diagnostics from `_httpDiagnostics(...)`.
- For non-200 responses, use `_withAuthorizationWarning(suggestions, status, usesApiKey=...)` to append the standard 401/403 hint about Roblox API key expiry.
- For external commands, prefer `runCommand([...], step=..., suggestions=[...])` over raw `subprocess.run` — it already converts `FileNotFoundError`, non-zero exit, etc. into `SGFLError` with command diagnostics.
- For Luau scripts, use `runLuauFile("lua/foo.luau")` — it resolves the package-relative path and forwards the package directory as `process.args[1]`.

## Conventions

- **camelCase for Python identifiers** (`startPlace`, `getEnvSafe`, `maskSecret`). Unusual for Python but consistent throughout — match the existing style; do not "Pythonify" to snake_case.
- **`announceStep("...")`** before each user-visible phase (prints a cyan `INFO` line). Successful task ends print a green `SUCCESS` line.
- **No tests, no lint config, no CI** beyond `.github/agents/`. Builds via `hatchling`. Distribution via `pipx install git+https://github.com/devvf/sgfl.git`; users upgrade with `pipx upgrade sgfl`.
- Bump the `version` in [pyproject.toml](pyproject.toml) when publishing user-visible changes (recent commits: `b6b6dd7 Bump version`).
- StyLua targets latest release ([.vscode/settings.json](.vscode/settings.json)).

## Cross-Platform Notes

- `start` opens Studio on Windows and macOS; **Linux is silently a no-op** for that step.
- VS Code launch uses `shell=True` (`["code", "."]`) — required on Windows where `code` is a `.cmd` shim.
- `publish` is fully cross-platform: it spawns no platform-specific subprocesses, uses ASCII-only output (`->` not `→`), uses stdlib `os.path.*` + `datetime` + `re` only, and reads input via `input()` after `sys.stdin.isatty()`. Works identically on Windows, macOS, and Linux.
- All Roblox HTTP calls use a 30s timeout (`REQUEST_TIMEOUT_SECONDS` in [operations.py](src/sgfl/operations.py)).

## When Modifying This Project

- Touching `assets.json` semantics? Update both [lua/build.luau](src/sgfl/lua/build.luau) (load) and [lua/importAssets.luau](src/sgfl/lua/importAssets.luau) (save) — they must stay symmetric, plus [lua/shared.luau](src/sgfl/lua/shared.luau) which both consume.
- Adding a new env var? Add it to `API_ENV_KEYS` or `ID_ENV_KEYS` in [util.py](src/sgfl/util.py) so detailed diagnostics format/validate it correctly, and add it to `envDiagnosticKeys` for the relevant subcommand in [sgfl.py](src/sgfl/sgfl.py). If the new key is per-developer (not per-environment), also add it to `CREDENTIAL_KEYS` so `getEnvSafe`'s missing-value hint points users at `sgfl auth login` and so `auth status` audits it.
- Adding a new subcommand? Wire it in four places: a Typer-decorated function in [sgfl.py](src/sgfl/sgfl.py) using the shared `_runTask(...)` helper, an `app.command()(yourCmd)` registration in [cli.py](src/sgfl/cli.py), an implementation in [operations.py](src/sgfl/operations.py), and the required-tool list in `_toolDiagnosticsForTask` in [util.py](src/sgfl/util.py).
- Adding a new flag to `publish`? Wire it in three places: a `typer.Option` parameter on `publish(...)` in [sgfl.py](src/sgfl/sgfl.py), forward it to `publishPlaces(...)` in the `_runTask` lambda, and consume it in `publishPlaces` in [operations.py](src/sgfl/operations.py).
- Don't introduce a hard dependency on Linux Studio launching — it's deliberately unsupported.
