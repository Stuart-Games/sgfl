import typer

from .sgfl import start

app = typer.Typer(pretty_exceptions_enable=False)
app.command()(start)

if __name__ == "__main__":
    app()
