import pytest


@pytest.fixture
def isolatedCwd(tmp_path, monkeypatch):
    """Chdir into an empty tmp dir. util.getFileURI()/getAbsoluteFileURI() calls
    Path.cwd() fresh on every call, so functions built on it (collectEntryFiles,
    runProjectionSave's writes, ...) automatically respect this."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
