import typer
from typing import Optional, Callable
from typing_extensions import Annotated
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
        envDiagnosticKeys=["PLACE_ID", "UNIVERSE_ID", "PUBLISH_KEY", "USER_ID"],
        fn=lambda: startPlace(pull),
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
        envDiagnosticKeys=["PLACE_ID", "DOWNLOAD_KEY"],
        fn=savePlace,
    )


def init():
    _runTask(
        "init",
        detailed=False,
        pullEnabled=False,
        envDiagnosticKeys=[],
        fn=initPlace,
    )


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
            help="Validate config and build the place file, but skip the confirmation prompt and the upload.",
        ),
    ] = False,
    noBuild: Annotated[
        bool,
        typer.Option(
            "--no-build",
            help="Skip lua/build.luau and reuse the existing Place.rbxl in the project root.",
        ),
    ] = False,
    places: Annotated[
        Optional[str],
        typer.Option(
            "--places",
            help="Comma-separated subset of place names to publish (e.g. 'lobby,arena'). Default: every PLACE_ID_<NAME> in the env file.",
        ),
    ] = None,
    versionType: Annotated[
        str,
        typer.Option(
            "--version-type",
            help="Roblox version type to publish as. Either 'Published' or 'Saved'.",
        ),
    ] = "Published",
):
    if versionType not in ("Published", "Saved"):
        raise typer.BadParameter(
            "--version-type must be either 'Published' or 'Saved'."
        )

    placesFilter: Optional[list[str]] = None
    if places:
        placesFilter = [p.strip() for p in places.split(",") if p.strip()]
        if not placesFilter:
            raise typer.BadParameter(
                "--places was provided but contained no usable names."
            )

    loadCredentials()

    _runTask(
        "publish",
        detailed=detailed,
        pullEnabled=False,
        envDiagnosticKeys=["UNIVERSE_ID", "PUBLISH_KEY"],
        fn=lambda: publishPlaces(
            env,
            dryRun=dryRun,
            noBuild=noBuild,
            placesFilter=placesFilter,
            versionType=versionType,
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
