import typer
from typing_extensions import Annotated
import dotenv
from .util import *
from .operations import *


def start(
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
    task: Annotated[
        str, typer.Argument(help="The (start/save/init) task to perform.")
    ] = None,
):
    if not task:
        raise typer.BadParameter("Missing task. Valid tasks are: start, save, init.")

    # env variables
    dotenv.load_dotenv(getFileURI(".env"))

    envDiagnosticKeys: list[str] = []
    if task == "start":
        envDiagnosticKeys = ["PLACE_ID", "UNIVERSE_ID", "PUBLISH_KEY", "USER_ID"]
    elif task == "save":
        envDiagnosticKeys = ["PLACE_ID", "DOWNLOAD_KEY"]

    try:
        startTask = task == "start"
        saveTask = task == "save"
        initTask = task == "init"

        if startTask:
            startPlace(pull)
        elif saveTask:
            savePlace()
        elif initTask:
            initPlace()
        else:
            raise typer.BadParameter(
                "Invalid task. Valid tasks are: start, save, init."
            )
    except typer.BadParameter:
        raise
    except SGFLError as err:
        printSgflError(
            err,
            includeEnvDiagnostics=detailed,
            envDiagnosticKeys=envDiagnosticKeys,
            includeDetailedDiagnostics=detailed,
            taskName=task,
            pullEnabled=pull,
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
            taskName=task,
            pullEnabled=pull,
        )
        raise typer.Exit(code=1)
