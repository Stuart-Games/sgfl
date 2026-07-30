import typer
from typing import Annotated, Optional, Callable
from .util import *
from .operations import *


def _runTask(
    taskName: str,
    *,
    detailed: bool,
    pullEnabled: bool,
    envDiagnosticKeys: list[str],
    fn: Callable[[], None],
):
    if taskName != "update":
        checkForUpdates()
    try:
        fn()
    except typer.BadParameter:
        raise
    except typer.Exit:
        raise
    except SGFLError as err:
        printSgflError(
            err,
            includeEnvDiagnostics=detailed,
            envDiagnosticKeys=envDiagnosticKeys,
            includeDetailedDiagnostics=detailed,
            taskName=taskName,
            pullEnabled=pullEnabled,
        )
        raise typer.Exit(code=1)
    except Exception as err:
        printSgflError(
            SGFLError(
                "Unexpected internal failure.",
                details=str(err),
                suggestions=[
                    "Re-run the command with the same arguments to confirm reproducibility.",
                    "If this keeps happening, report the command and full error text to maintainers.",
                ],
            ),
            includeEnvDiagnostics=detailed,
            envDiagnosticKeys=envDiagnosticKeys,
            includeDetailedDiagnostics=detailed,
            taskName=taskName,
            pullEnabled=pullEnabled,
        )
        raise typer.Exit(code=1)


def _loadEnv(env: Optional[str], envSuffix: Optional[str]) -> None:
    """Layered env load. Order: ~/.sgfl/credentials -> .env (or .env.<env>).

    - Credentials file is always tried first (silent if missing) so per-developer
      keys (PUBLISH_KEY/DOWNLOAD_KEY/USER_ID) are available.
    - If `env` is given, .env.<env> is loaded with override=True. The file is
      required (hard fail if missing). `.env` is NOT loaded in this case to
      keep environment isolation explicit.
    - If `env` is None, .env is loaded if present (silent if missing — matches
      legacy behavior).
    - `envSuffix` (deprecated) only applies when no `env` arg is given.
    """
    loadCredentials()

    if env:
        loadEnvFile(env)
    else:
        loadBaseEnv()
        if envSuffix:
            setEnvSuffix(envSuffix)


def start(
    env: Annotated[
        Optional[str],
        typer.Argument(
            help="Optional env name. If given, loads .env.<env> instead of .env (e.g. 'testing' loads .env.testing).",
        ),
    ] = None,
    pull: Annotated[
        bool, typer.Option("--pull", "-p", help="Whether to git pull on start.")
    ] = False,
    port: Annotated[
        Optional[int],
        typer.Option(
            "--port",
            help="Port for rojo serve, used exactly as given. Without it, the next free port "
            "upward from the default is picked automatically (with a loud notice) when the "
            "default is taken — e.g. by another sgfl start's rojo serve.",
        ),
    ] = None,
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed .env and API key diagnostics if an error occurs.",
        ),
    ] = False,
    envSuffix: Annotated[
        Optional[str],
        typer.Option(
            "--env-suffix",
            "-e",
            help="[DEPRECATED] Suffix appended to env var names. Prefer the positional <env> arg with .env.<env> files.",
        ),
    ] = None,
):
    if env and envSuffix:
        raise typer.BadParameter(
            "Pass either the positional <env> arg or --env-suffix, not both."
        )
    _loadEnv(env, envSuffix)
    _runTask(
        "start",
        detailed=detailed,
        pullEnabled=pull,
        envDiagnosticKeys=[
            "PLACE_ID",
            "UNIVERSE_ID",
            "PUBLISH_KEY",
            "DOWNLOAD_KEY",
            "EXECUTION_KEY",
            "USER_ID",
        ],
        fn=lambda: startPlace(pull, servePort=port),
    )


def save(
    env: Annotated[
        Optional[str],
        typer.Argument(
            help="Optional env name. If given, loads .env.<env> instead of .env.",
        ),
    ] = None,
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed .env and API key diagnostics if an error occurs.",
        ),
    ] = False,
    envSuffix: Annotated[
        Optional[str],
        typer.Option(
            "--env-suffix",
            "-e",
            help="[DEPRECATED] Suffix appended to env var names. Prefer the positional <env> arg with .env.<env> files.",
        ),
    ] = None,
):
    if env and envSuffix:
        raise typer.BadParameter(
            "Pass either the positional <env> arg or --env-suffix, not both."
        )
    _loadEnv(env, envSuffix)
    _runTask(
        "save",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=["PLACE_ID", "UNIVERSE_ID", "DOWNLOAD_KEY", "EXECUTION_KEY"],
        fn=savePlace,
    )


def migrate(
    env: Annotated[
        Optional[str],
        typer.Argument(
            help="Optional env name. If given, loads .env.<env> instead of .env.",
        ),
    ] = None,
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed .env and API key diagnostics if an error occurs.",
        ),
    ] = False,
):
    """Convert a legacy (.rbxm) project to the new cloud-pipeline format."""
    _loadEnv(env, None)
    _runTask(
        "migrate",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=["PLACE_ID", "UNIVERSE_ID", "DOWNLOAD_KEY", "EXECUTION_KEY"],
        fn=migratePlace,
    )


def init():
    _runTask(
        "init",
        detailed=False,
        pullEnabled=False,
        envDiagnosticKeys=[],
        fn=initPlace,
    )


def build(
    env: Annotated[
        str,
        typer.Argument(
            help="Env name. Loads .env.<env> on top of ~/.sgfl/credentials.",
        ),
    ],
    out: Annotated[
        str,
        typer.Option(
            "--out",
            "-o",
            help="Where to write the finished place file.",
        ),
    ] = "dist/place.rbxl",
    noBuild: Annotated[
        bool,
        typer.Option(
            "--no-build",
            help="Skip rojo and reuse the existing Place.rbxl in the project root (it is not deleted afterwards).",
        ),
    ] = False,
    failOnWarn: Annotated[
        bool,
        typer.Option(
            "--fail-on-warn",
            help=(
                "Fail if anything warned that the output may not match the repo "
                "(a blob that did not deserialize, an unwritable sidecar property, "
                "an inferred version). Informational warnings are ignored."
            ),
        ),
    ] = False,
    detailed: Annotated[
        bool,
        typer.Option("--detailed", "-d", help="Print detailed .env and HTTP diagnostics if an error occurs."),
    ] = False,
):
    """Build the final place file without publishing it.

    Runs rojo plus the cloud apply against the scratch build place
    (PLACE_ID_BUILD) and writes sidecar-patched bytes to --out. Publish them
    later with `sgfl upload`. Safe to run unattended: no live place is touched.

    Resolves no publish targets, so with UNIVERSE_ID_BUILD set it needs no
    place ID belonging to the game at all.
    """
    loadCredentials()
    _runTask(
        "build",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[
            "UNIVERSE_ID",
            "UNIVERSE_ID_BUILD",
            "PUBLISH_KEY",
            "DOWNLOAD_KEY",
            "EXECUTION_KEY",
        ],
        fn=lambda: buildArtifact(env, outPath=out, noBuild=noBuild, failOnWarn=failOnWarn),
    )


def preflightCmd(
    env: Annotated[
        str,
        typer.Argument(
            help="Env name. Loads .env.<env> on top of ~/.sgfl/credentials.",
        ),
    ],
    detailed: Annotated[
        bool,
        typer.Option("--detailed", "-d", help="Print detailed .env and HTTP diagnostics if an error occurs."),
    ] = False,
):
    """Check every credential is present and still alive, then stop.

    Does no work — no rojo, no execution task, no upload — so it is free to run
    first in CI. An expired key fails here in seconds, named, instead of
    several minutes into a build as "User unauthorized to update place".
    """
    loadCredentials()
    _runTask(
        "preflight",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[
            "UNIVERSE_ID",
            "UNIVERSE_ID_BUILD",
            "PUBLISH_KEY",
            "DOWNLOAD_KEY",
            "EXECUTION_KEY",
        ],
        fn=lambda: preflight(env),
    )


def upload(
    artifact: Annotated[
        str,
        typer.Argument(help="Path to a place file produced by sgfl build."),
    ],
    env: Annotated[
        str,
        typer.Argument(
            help="Env name. Loads .env.<env> on top of ~/.sgfl/credentials.",
        ),
    ],
    places: Annotated[
        Optional[str],
        typer.Option(
            "--places",
            help="Comma-separated subset of place names to publish (e.g. 'lobby,arena'). Default: every PLACE_ID_<NAME> in the env file.",
        ),
    ] = None,
    expectPlaces: Annotated[
        Optional[str],
        typer.Option(
            "--expect-places",
            help="Comma-separated place names the caller expects to publish to. Aborts on any mismatch. Required when SGFL_CI is set.",
        ),
    ] = None,
    versionType: Annotated[
        str,
        typer.Option("--version-type", help="Roblox version type. Either 'Published' or 'Saved'."),
    ] = "Published",
    jsonPath: Annotated[
        Optional[str],
        typer.Option("--json", help="Write a machine-readable result (per-place version numbers) to this path."),
    ] = None,
    detailed: Annotated[
        bool,
        typer.Option("--detailed", "-d", help="Print detailed .env and HTTP diagnostics if an error occurs."),
    ] = False,
):
    """Publish an already-built place file to every declared place.

    Uploads only — no rojo, no execution task, no rate limit — so a partial
    failure is safe to re-run with the identical artifact.
    """
    _validateVersionType(versionType)
    loadCredentials()
    _runTask(
        "upload",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=["UNIVERSE_ID", "PUBLISH_KEY"],
        fn=lambda: uploadArtifact(
            env,
            artifactPath=artifact,
            placesFilter=_splitNames(places, "--places"),
            versionType=versionType,
            expectPlaces=_splitNames(expectPlaces, "--expect-places"),
            jsonPath=jsonPath,
        ),
    )


def _validateVersionType(versionType: str) -> None:
    if versionType not in ("Published", "Saved"):
        raise typer.BadParameter("--version-type must be either 'Published' or 'Saved'.")


def _splitNames(raw: Optional[str], flagName: str) -> Optional[list[str]]:
    if raw is None:
        return None
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names:
        raise typer.BadParameter(f"{flagName} was provided but contained no usable names.")
    return names


def publish(
    env: Annotated[
        str,
        typer.Argument(
            help="Env name. Loads .env.<env> on top of ~/.sgfl/credentials (e.g. 'testing' loads .env.testing).",
        ),
    ],
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed .env and HTTP diagnostics if an error occurs.",
        ),
    ] = False,
    dryRun: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run the full build (rojo + cloud apply + sidecar patch) and stop before uploading.",
        ),
    ] = False,
    noBuild: Annotated[
        bool,
        typer.Option(
            "--no-build",
            help="Skip rojo and reuse the existing Place.rbxl in the project root (it is not deleted afterwards).",
        ),
    ] = False,
    places: Annotated[
        Optional[str],
        typer.Option(
            "--places",
            help="Comma-separated subset of place names to publish (e.g. 'lobby,arena'). Default: every PLACE_ID_<NAME> in the env file.",
        ),
    ] = None,
    expectPlaces: Annotated[
        Optional[str],
        typer.Option(
            "--expect-places",
            help="Comma-separated place names the caller expects to publish to. Aborts on any mismatch. Required when SGFL_CI is set.",
        ),
    ] = None,
    versionType: Annotated[
        str,
        typer.Option(
            "--version-type",
            help="Roblox version type to publish as. Either 'Published' or 'Saved'.",
        ),
    ] = "Published",
    failOnWarn: Annotated[
        bool,
        typer.Option(
            "--fail-on-warn",
            help=(
                "Fail before uploading if anything warned that the build may not "
                "match the repo. Informational warnings are ignored."
            ),
        ),
    ] = False,
    jsonPath: Annotated[
        Optional[str],
        typer.Option("--json", help="Write a machine-readable result (per-place version numbers) to this path."),
    ] = None,
):
    _validateVersionType(versionType)

    loadCredentials()

    _runTask(
        "publish",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[
            "UNIVERSE_ID",
            "UNIVERSE_ID_BUILD",
            "PUBLISH_KEY",
            "DOWNLOAD_KEY",
            "EXECUTION_KEY",
        ],
        fn=lambda: publishPlaces(
            env,
            dryRun=dryRun,
            noBuild=noBuild,
            placesFilter=_splitNames(places, "--places"),
            versionType=versionType,
            expectPlaces=_splitNames(expectPlaces, "--expect-places"),
            jsonPath=jsonPath,
            failOnWarn=failOnWarn,
        ),
    )


def authLoginCmd(
    publishKey: Annotated[
        Optional[str],
        typer.Option(
            "--publish-key",
            help="Set PUBLISH_KEY non-interactively. Combine with the other flags to script setup.",
        ),
    ] = None,
    downloadKey: Annotated[
        Optional[str],
        typer.Option("--download-key", help="Set DOWNLOAD_KEY non-interactively."),
    ] = None,
    executionKey: Annotated[
        Optional[str],
        typer.Option(
            "--execution-key",
            help="Set EXECUTION_KEY (Open Cloud key with the Luau Execution Sessions system) non-interactively.",
        ),
    ] = None,
    userId: Annotated[
        Optional[str],
        typer.Option("--user-id", help="Set USER_ID non-interactively."),
    ] = None,
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed diagnostics if an error occurs.",
        ),
    ] = False,
):
    _runTask(
        "auth-login",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[],
        fn=lambda: authLogin(
            publishKey=publishKey,
            downloadKey=downloadKey,
            executionKey=executionKey,
            userId=userId,
        ),
    )


def update(
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed diagnostics if an error occurs.",
        ),
    ] = False,
):
    _runTask(
        "update",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[],
        fn=runUpdate,
    )


def authStatusCmd(
    detailed: Annotated[
        bool,
        typer.Option(
            "--detailed",
            "-d",
            help="Print detailed diagnostics if an error occurs.",
        ),
    ] = False,
):
    _runTask(
        "auth-status",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=[],
        fn=authStatus,
    )
