import typer

from sgfl import sgfl

app = typer.Typer()
app.command()(sgfl) # type: ignore

if __name__ == "__main__":
    app()