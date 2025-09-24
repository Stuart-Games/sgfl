import typer
from typing_extensions import Annotated
import dotenv
from .util import *
from .operations import *

def start(
    pull: Annotated[bool,typer.Option("--pull","-p",help="Whether to git pull on start.")] = False,
    task: Annotated[str, typer.Argument(help="The (start/save/init) task to perform.")] = None,
):
    if not task:
        print("Incorrect usage, run sgfl --help to see all commands.")
        return

    #env variables
    dotenv.load_dotenv(getFileURI(".env"))


    start = task == "start"
    save = task == "save"
    init = task == "init"

    if start:
        startPlace(pull)
    elif save:
        savePlace()
    elif init:
        initPlace()
    else:
        print("Correct usage: sgfl start | save | init")
