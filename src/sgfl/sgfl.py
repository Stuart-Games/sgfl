import typer
from typing_extensions import Annotated
import dotenv
from .util import *
from .operations import *


def start(
    pull: Annotated[
        bool, typer.Option("--pull", "-p", help="Whether to git pull on start.")
    ] = False,
    task: Annotated[
        str, typer.Argument(help="The (start/save/init) task to perform.")
    ] = None,
):
    if not task:
        raise typer.BadParameter("Missing task. Valid tasks are: start, save, init.")

    # env variables
    dotenv.load_dotenv(getFileURI(".env"))

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
        printSgflError(err)
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
            )
        )
        raise typer.Exit(code=1)
