# sgfl — Stuart Games Roblox workflow

sgfl keeps a whole Roblox game in git — scripts, map, UI, models, settings — and gives you two commands to work on it:

- **`sgfl start`** builds the game from the repo, publishes it to your dev place, and opens Roblox Studio and your code editor with live script sync.
- **`sgfl save`** writes what you changed in Studio back into the repo, as files you can diff and commit.

```
git pull → sgfl start → work in Studio + your editor → sgfl save → git commit
```

## The tools, in one table

You don't need to know the Roblox ecosystem. This is everything sgfl touches:

| Thing | What it is |
|---|---|
| **Roblox Studio** | Roblox's editor. Maps, UI and models are built here. |
| **Place / Universe** | A *place* is one level file; a *universe* (a.k.a. *experience*) is the game that contains places. Each has a numeric id. |
| **[Rojo](https://rojo.space/)** | Syncs `.luau` script files on disk into Studio live, so you write code in a real editor. sgfl runs it for you. |
| **[Rokit](https://github.com/rojo-rbx/rokit)** | Installs the command-line tools a project needs (Rojo, Wally, …) at the exact versions pinned in the project's `rokit.toml`. Think `nvm`/`rustup`. |
| **[Wally](https://wally.run/)** | Package manager for Roblox libraries (think `npm`). Dependencies are listed in `wally.toml` and installed into `Packages/`. sgfl runs `wally install` automatically when needed — you never have to. |
| **[Git LFS](https://git-lfs.com/)** | Git extension for large binary files. Models and maps are stored through it. **Without it you clone tiny placeholder files instead of the game** — see [Git LFS](#git-lfs). |
| **Open Cloud API key** | A Roblox access token. sgfl uses keys to publish, download and run code against your place in Roblox's cloud. |
| **`.sgfl` files** | sgfl's text format for everything that isn't a script. Which parts of the game are saved is declared in `assets.json`. |

## Setup (once per machine)

**1. Install the prerequisites**

- [Roblox Studio](https://create.roblox.com/) — and sign in.
- Python 3.9+ and [pipx](https://pipx.pypa.io/stable/installation/).
- Git and [Git LFS](https://git-lfs.com/) (`brew install git-lfs` on macOS; bundled with Git for Windows). Then, once:

  ```bash
  git lfs install
  ```

- [Rokit](https://github.com/rojo-rbx/rokit#installation). macOS/Linux:

  ```bash
  curl -sSf https://raw.githubusercontent.com/rojo-rbx/rokit/main/scripts/install.sh | bash
  ```

  Windows (PowerShell):

  ```powershell
  Invoke-RestMethod https://raw.githubusercontent.com/rojo-rbx/rokit/main/scripts/install.ps1 | Invoke-Expression
  ```

  Open a new terminal afterwards so `rokit` is on your PATH.

**2. Install sgfl**

```bash
pipx install git+https://github.com/Stuart-Games/sgfl.git
```

Update later with `sgfl update`.

**3. Create your API key and log in**

You need edit access to the game first (ask the owner to add you to the group or experience).

Create a key at [Creator Dashboard → API Keys](https://create.roblox.com/dashboard/credentials). Add the experience you're working on and grant it:

| Permission | Used for |
|---|---|
| Place publishing (write) | uploading the built place |
| Asset delivery (download) + Assets read | downloading the place back |
| Luau Execution Sessions (read + write) | running the save/apply task inside Roblox's engine |

Set accepted IPs to `0.0.0.0/0` unless you have a fixed address. Then:

```bash
sgfl auth login
```

It asks for `PUBLISH_KEY`, `DOWNLOAD_KEY`, `EXECUTION_KEY` and `USER_ID`. One key with all of the permissions above can be pasted into all three key prompts; separate keys only matter for CI. `USER_ID` is the number in your profile URL (`roblox.com/users/<id>/profile`). Check the result any time with `sgfl auth status`.

Keys live in `~/.sgfl/credentials`, never in a project. Roblox disables keys after 60 days without use — re-enable them in the dashboard.

## Join an existing project

```bash
git clone --recurse-submodules <repo-url>
```

Then, inside the project:

```bash
git lfs pull
```

```bash
rokit install
```

```bash
rojo plugin install
```

- `--recurse-submodules` fetches shared libraries that live in their own repos (forgot it? `git submodule update --init --recursive`).
- `git lfs pull` makes sure the real model/map files are present, not placeholders.
- `rokit install` installs the project's pinned tools; say yes if it asks to trust them.
- `rojo plugin install` adds the Rojo plugin to Studio (once per machine).

**Tell sgfl which place to use.** Create a `.env` file in the project root (it is gitignored — it's yours):

```
PLACE_ID=1234567890
UNIVERSE_ID=9876543210
```

Both ids are in the [Creator Dashboard](https://create.roblox.com/dashboard/creations): on the experience's tile, **⋯ → Copy Universe ID** and **Copy Start Place ID**. If the project commits shared environment files such as `.env.testing`, skip the `.env` and pass the name instead: `sgfl start testing`.

Now run:

```bash
sgfl start
```

When Studio opens, click **Rojo → Connect** in the Plugins tab. You're in.

## Daily loop

| Step | Command |
|---|---|
| Get the latest and open everything | `sgfl start -p` (`-p` = `git pull` first) |
| Edit scripts | In your editor, under `src/`. They appear in Studio instantly. |
| Edit the map, UI, models | In Studio. Save with **File → Save to Roblox** (or publish). |
| Write Studio changes to the repo | `sgfl save` |
| Share it | `git add -A && git commit && git push` |

Two rules:

- **Scripts are edited on disk, never in Studio** — Rojo overwrites Studio's copy.
- **Everything else is edited in Studio, then `sgfl save`d** — if you don't save and commit, the next `sgfl start` rebuilds the place from the repo and your Studio work is gone.

`sgfl start` takes a few minutes: the place is assembled inside a Roblox cloud engine, not on your machine.

Prefer another editor? `sgfl config set EDITOR_COMMAND "cursor ."` (or `none` to open nothing). The default is `code .` (VS Code).

## Start a new project

In an empty directory:

```bash
sgfl init
```

It asks for a name, `PLACE_ID` and `UNIVERSE_ID`, then scaffolds `src/`, `assets.json`, `default.project.json`, `.env`, and a Rokit toolchain with Rojo. Then set up git and LFS before your first commit:

```bash
git init && git lfs track "*.rbxm" && git add .gitattributes
```

Run `sgfl save` once to pull the place's current contents into the repo. To use Wally packages, `rokit add UpliftGames/wally`, then `wally init` and list dependencies in `wally.toml`.

## Git LFS

Blob entries (`*.sgfl.rbxm` — the map and model containers) are binary and large, so projects track `*.rbxm` with Git LFS. If LFS isn't installed when you clone, those files arrive as ~130-byte text placeholders and **the game builds without its map and models**.

Check: `git lfs ls-files` should list the `.rbxm` files, and they should be far larger than 1 KB. Fix: install Git LFS, then run `git lfs install` and `git lfs pull`.

## Troubleshooting

Add `-d` for full diagnostics on any failure: `sgfl -d start`.

| Symptom | Cause and fix |
|---|---|
| Map/models missing after `start`; warning that blob entries arrived empty | Git LFS placeholders. See [Git LFS](#git-lfs). |
| `401`, `403`, or `User unauthorized to update place` | Key expired, missing a permission, not scoped to this experience, or your IP isn't allowed. Check the key in the dashboard; `sgfl auth status` shows what sgfl has. |
| `rojo` / `wally` not found | Run `rokit install` in the project, in a fresh terminal. |
| Scripts don't sync into Studio | Click **Connect** in the Rojo plugin. If sgfl printed that it picked a different port, enter that port in the plugin first. |
| Entry files rejected for CRLF line endings (Windows) | Git converted them on checkout. Make sure `.gitattributes` has `*.sgfl text eol=lf`, then run the re-checkout command the error prints. |
| `sgfl save` warns an entry "came back empty" | A previous `start`/`publish` died partway and left the place without assets. Answer **NO**, run `sgfl start` again, then save. |
| "No `assets.json`" error | You're not in the project root. |
| Refuses to run, mentions `sgfl migrate` | The project predates sgfl 2.0. Run `sgfl migrate` once (port any `postbuild.luau` to `postapply.luau`). |
| Two people ran `start`/`publish` on one place at once | sgfl aborts rather than ship the wrong build. Take turns, or use a dev place each. |

## Command reference

| Command | Does |
|---|---|
| `sgfl start [env] [-p] [--port N]` | Build, publish to the dev place, open Studio + editor, `rojo serve` |
| `sgfl save [env]` | Write the place's current state into the repo |
| `sgfl init` | Scaffold a new project |
| `sgfl auth login` / `auth status` | Manage your keys (`~/.sgfl/credentials`) |
| `sgfl config list` / `set` / `unset` | Manage your preferences (`~/.sgfl/config`) |
| `sgfl update` | Upgrade sgfl |
| `sgfl publish <env>` | Build and publish to every place in `.env.<env>` |
| `sgfl build` / `upload` / `preflight` | The CI pieces of `publish` |
| `sgfl migrate` | One-time conversion of a pre-2.0 project |

`sgfl <command> --help` lists every flag.

## Going further

- **[docs/assets-json.md](docs/assets-json.md)** — choosing what gets saved: entries, text vs blob, per-child files, include/exclude, the `postapply.luau` hook.
- **[docs/publishing-and-ci.md](docs/publishing-and-ci.md)** — `.env.<env>` files, `sgfl publish`, CI with `build`/`upload`, organization setup.

`Place.rbxl` is a temporary build artifact; never commit it.
