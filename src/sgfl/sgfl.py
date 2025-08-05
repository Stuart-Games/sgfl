import typer
from save import savePlace
from start import startPlace
from typing_extensions import Annotated

def sgfl(
    task: Annotated[str, typer.Argument(help="The (start/save) task to perform")] = "",
):
    start = task == "start"
    save = task == "save"

    if start:
        startPlace()
    elif save:
        savePlace()
    else:
        print("Correct usage: sgfl start or sgfl save")
