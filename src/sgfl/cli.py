import sys

import typer

from .sgfl import (
    start,
    save,
    init,
    build,
    preflightCmd,
    upload,
    publish,
    migrate,
    update,
    authLoginCmd,
    authStatusCmd,
    configListCmd,
    configSetCmd,
    configUnsetCmd,
)

# Legacy Windows consoles decode stdout as cp1252, which cannot encode the
# arrow glyphs in interactive prompts; replace rather than crash mid-task
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

app = typer.Typer(pretty_exceptions_enable=False)
app.command()(start)
app.command()(save)
app.command()(init)
app.command()(build)
app.command("preflight")(preflightCmd)
app.command()(upload)
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

configApp = typer.Typer(
    pretty_exceptions_enable=False,
    help="Manage per-developer preferences stored at ~/.sgfl/config (not secrets — see sgfl auth for those).",
)
configApp.command("list")(configListCmd)
configApp.command("set")(configSetCmd)
configApp.command("unset")(configUnsetCmd)
app.add_typer(configApp, name="config")

if __name__ == "__main__":
    app()
