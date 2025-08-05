import typer
from typing_extensions import Annotated


def sgfl(
    name: Annotated[str, typer.Argument(help="The (last, if --gender is given) name of the person to greet")] = "",
    gender: Annotated[str, typer.Option(help="The gender of the person to greet")] = "",
    knight: Annotated[bool, typer.Option(help="Whether the person is a knight")] = False,
    count: Annotated[int, typer.Option(help="Number of times to greet the person")] = 1
):
    greeting = "Greetings, dear "
    masculine = gender == "masculine"
    feminine = gender == "feminine"
    if gender or knight:
        salutation = ""
        if knight:
            salutation = "Sir "
        elif masculine:
            salutation = "Mr. "
        elif feminine:
            salutation = "Ms. "
        greeting += salutation
        if name:
            greeting += f"{name}!"
        else:
            pronoun = "her" if feminine else "his" if masculine or knight else "its"
            greeting += f"what's-{pronoun}-name"
    else:
        if name:
            greeting += f"{name}!"
        elif not gender:
            greeting += "friend!"
    for _ in range(0, count):
        print(greeting)