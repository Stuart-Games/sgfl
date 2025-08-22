import typer
from typing_extensions import Annotated
import dotenv
from .util import *
from .operations import *

def start(
    pull: Annotated[bool,typer.Option("--pull","-p",help="Whether to git pull on start.")] = False,
    task: Annotated[str, typer.Argument(help="The (start/save) task to perform.")] = None,
):
    if not task:
        print("Incorrect usage, run sgfl --help to see all commands.")
        return

    
    if pull:
        print("P ENABLE")
    
    PLACE_FILE_PATH = getFileURI("Place.rbxlx")

    #env variables
    dotenv.load_dotenv(getFileURI(".env"))
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    userId = getEnvSafe("USER_ID")

    start = task == "start"
    save = task == "save"

    if start:
        startPlace(userId,placeId,universeId,publishKey,PLACE_FILE_PATH,pull)
    elif save:
        savePlace(placeId,downloadKey,PLACE_FILE_PATH)
    else:
        print("Correct usage: sgfl start or sgfl save")
