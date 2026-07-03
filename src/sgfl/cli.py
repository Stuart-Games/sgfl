import typer

from .sgfl import start, save, init, publish, migrate, update, authLoginCmd, authStatusCmd

app = typer.Typer(pretty_exceptions_enable=False)
app.command()(start)
app.command()(save)
app.command()(init)
app.command()(publish)
app.command()(migrate)
app.command()(update)

authApp = typer.Typer(
    pretty_exceptions_enable=False,
    help="Manage per-developer credentials stored at ~/.sgfl/credentials.",
)
authApp.command("login")(authLoginCmd)
authApp.command("status")(authStatusCmd)
app.add_typer(authApp, name="auth")

if __name__ == "__main__":
    app()
