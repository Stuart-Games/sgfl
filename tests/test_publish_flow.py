"""Coverage for the build/upload split and the CI authorization path.

None of these hit the network: they exercise target resolution, the SGFL_CI
guard rails, artifact validation, env precedence, and retry classification.
"""

import pytest
import typer

from sgfl import cloud, operations, util
from sgfl.util import SGFLError


@pytest.fixture
def cleanEnv(monkeypatch):
    """Drop every PLACE_ID* var so a developer's real shell can't leak in."""
    for key in list(__import__("os").environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SGFL_CI", raising=False)
    return monkeypatch


# --- target resolution -----------------------------------------------------


def test_build_place_is_not_a_publish_target(cleanEnv):
    cleanEnv.setenv("PLACE_ID_MAIN", "111")
    cleanEnv.setenv("PLACE_ID_BUILD", "999")

    targets, buildPlaceId = operations._resolvePublishTargets(None)

    assert targets == {"main": "111"}
    assert buildPlaceId == "999"


def test_build_place_alone_is_not_publishable(cleanEnv):
    cleanEnv.setenv("PLACE_ID_BUILD", "999")

    with pytest.raises(SGFLError) as excinfo:
        operations._resolvePublishTargets(None)

    assert "No publish targets" in excinfo.value.message


def test_places_filter_rejects_the_reserved_build_name(cleanEnv):
    cleanEnv.setenv("PLACE_ID_MAIN", "111")
    cleanEnv.setenv("PLACE_ID_BUILD", "999")

    with pytest.raises(SGFLError) as excinfo:
        operations._resolvePublishTargets(["build"])

    assert "reserved" in excinfo.value.message


def test_base_place_prefers_the_scratch_build_place(cleanEnv):
    name, placeId = operations._resolveBasePlace({"main": "111"}, "999")

    assert (name, placeId) == (util.BUILD_PLACE_NAME, "999")


def test_base_place_falls_back_to_main_with_a_warning(cleanEnv, capsys):
    name, placeId = operations._resolveBasePlace({"lobby": "222", "main": "111"}, None)

    assert (name, placeId) == ("main", "111")
    assert "WARN" in capsys.readouterr().out


def test_ci_refuses_to_apply_against_a_live_place(cleanEnv):
    """The apply target holds an asset-less Saved version while the task runs,
    and keeps it if the task dies. Unattended, that must never be a live place."""
    cleanEnv.setenv("SGFL_CI", "1")

    with pytest.raises(SGFLError) as excinfo:
        operations._resolveBasePlace({"main": "111"}, None)

    assert "PLACE_ID_BUILD" in excinfo.value.message


# --- authorization ---------------------------------------------------------


def test_ci_requires_expect_places(cleanEnv):
    cleanEnv.setenv("SGFL_CI", "1")

    with pytest.raises(SGFLError) as excinfo:
        operations._confirmTargets("prod", [], {"main": "111"}, None)

    assert "--expect-places" in excinfo.value.message


def test_expect_places_mismatch_aborts(cleanEnv):
    cleanEnv.setenv("SGFL_CI", "1")

    with pytest.raises(SGFLError) as excinfo:
        operations._confirmTargets("prod", [], {"main": "111", "lobby": "222"}, ["main"])

    assert "resolved but not expected: lobby" in excinfo.value.details


def test_expect_places_match_proceeds_without_a_prompt(cleanEnv):
    cleanEnv.setenv("SGFL_CI", "1")

    operations._confirmTargets("prod", [], {"main": "111", "lobby": "222"}, ["Main", " lobby "])


def test_expect_places_is_checked_even_interactively(cleanEnv):
    """The mismatch guard runs before the TTY check, so a wrong --expect-places
    fails the same way at a terminal as it does in CI."""
    with pytest.raises(SGFLError) as excinfo:
        operations._confirmTargets("prod", [], {"main": "111"}, ["lobby"])

    assert "--expect-places" in excinfo.value.message


# --- artifact handling -----------------------------------------------------


def test_artifact_must_be_a_binary_place_file(isolatedCwd):
    bad = isolatedCwd / "place.rbxl"
    bad.write_bytes(b"<roblox version=\"4\">not binary</roblox>")

    with pytest.raises(SGFLError) as excinfo:
        operations._readArtifact(str(bad))

    assert "not a binary .rbxl" in excinfo.value.message


def test_artifact_round_trips(isolatedCwd):
    good = isolatedCwd / "place.rbxl"
    good.write_bytes(operations.PLACE_FILE_MAGIC + b"\x89\xff\r\n\x1a\n\x00payload")

    assert operations._readArtifact(str(good)).endswith(b"payload")


def test_missing_artifact_names_the_build_command(isolatedCwd):
    with pytest.raises(SGFLError) as excinfo:
        operations._readArtifact(str(isolatedCwd / "nope.rbxl"))

    assert any("sgfl build" in s for s in excinfo.value.suggestions)


# --- env precedence --------------------------------------------------------


def test_process_env_outranks_the_committed_env_file(isolatedCwd, monkeypatch, capsys):
    """A CI secret must not be clobbered by a committed ID file."""
    (isolatedCwd / ".env.prod").write_text("PLACE_ID_MAIN=111\nUNIVERSE_ID=42\n")
    monkeypatch.setenv("PLACE_ID_MAIN", "999")
    monkeypatch.setattr(util, "_PROCESS_ENV_KEYS", frozenset({"PLACE_ID_MAIN"}))

    util.loadEnvFile("prod")

    import os

    assert os.environ["PLACE_ID_MAIN"] == "999"
    assert os.environ["UNIVERSE_ID"] == "42"  # not shadowed -> file value applies
    assert "take precedence" in capsys.readouterr().out


def test_missing_env_file_is_fatal_interactively(isolatedCwd, monkeypatch):
    monkeypatch.delenv("SGFL_CI", raising=False)

    with pytest.raises(SGFLError) as excinfo:
        util.loadEnvFile("prod")

    assert "Missing environment file" in excinfo.value.message


def test_missing_env_file_is_tolerated_in_ci(isolatedCwd, monkeypatch, capsys):
    monkeypatch.setenv("SGFL_CI", "1")

    assert util.loadEnvFile("prod") is None
    assert "WARN" in capsys.readouterr().out


# --- upload loop -----------------------------------------------------------


class FakeUploadResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.reason = "OK" if status == 200 else "Server Error"
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_upload_reports_every_place_version(monkeypatch, isolatedCwd):
    calls = []

    def fakePost(url, **kwargs):
        calls.append(url)
        return FakeUploadResponse(200, {"versionNumber": 100 + len(calls)})

    monkeypatch.setattr(operations.requests, "post", fakePost)

    record = operations._uploadToTargets(
        env="prod",
        universeId="42",
        publishKey="k" * 20,
        targets={"main": "111", "lobby": "222"},
        placeBinary=b"<roblox!bytes",
        versionType="Published",
    )

    assert len(calls) == 2
    assert record["ok"] is True
    assert [p["versionNumber"] for p in record["places"]] == [101, 102]
    # lobby sorts first, so it takes version 101
    assert record["places"][0]["name"] == "lobby"


def test_one_failed_place_does_not_stop_the_others(monkeypatch, isolatedCwd):
    """A partial failure must still attempt every target and still report the
    versions that did land — that record is what makes the re-run safe."""

    def fakePost(url, **kwargs):
        if "/places/222/" in url:
            return FakeUploadResponse(500, text="boom")
        return FakeUploadResponse(200, {"versionNumber": 7})

    monkeypatch.setattr(operations.requests, "post", fakePost)

    with pytest.raises(SGFLError) as excinfo:
        operations._uploadToTargets(
            env="prod",
            universeId="42",
            publishKey="k" * 20,
            targets={"main": "111", "lobby": "222"},
            placeBinary=b"<roblox!bytes",
            versionType="Published",
        )

    record = excinfo.value.record
    assert record["ok"] is False
    byName = {p["name"]: p for p in record["places"]}
    assert byName["main"]["versionNumber"] == 7
    assert byName["lobby"]["ok"] is False

    operations._writeJsonReport("dist/report.json", record)
    written = __import__("json").loads((isolatedCwd / "dist" / "report.json").read_text())
    assert written["places"] == record["places"]


# --- build artifact --------------------------------------------------------


@pytest.fixture
def stubbedBuild(monkeypatch, isolatedCwd, cleanEnv):
    """A buildArtifact run with every cloud call replaced."""
    (isolatedCwd / ".env.prod").write_text("UNIVERSE_ID=42\nPLACE_ID_MAIN=111\nPLACE_ID_BUILD=999\n")
    (isolatedCwd / "assets.json").write_text('{"Lighting": {"folder": "map", "robloxPath": "Lighting"}}')
    monkeypatch.setenv("PUBLISH_KEY", "p" * 20)
    monkeypatch.setenv("DOWNLOAD_KEY", "d" * 20)
    monkeypatch.setenv("EXECUTION_KEY", "e" * 20)

    seen = {}

    def fakeBuildFinalPlace(**kwargs):
        seen.update(kwargs)
        return operations.PLACE_FILE_MAGIC + b"applied"

    monkeypatch.setattr(operations.cloud, "buildFinalPlace", fakeBuildFinalPlace)
    monkeypatch.setattr(operations, "_runRojoBuild", lambda: None)
    # PLACE_FILE_PATH is resolved once at import, so it does not follow the
    # test's chdir the way getFileURI() calls do.
    monkeypatch.setattr(operations, "PLACE_FILE_PATH", str(isolatedCwd / "Place.rbxl"))
    return seen


def test_build_writes_the_artifact_and_targets_the_scratch_place(stubbedBuild, isolatedCwd):
    result = operations.buildArtifact("prod", outPath="dist/place.rbxl")

    assert stubbedBuild["basePlaceId"] == "999"  # never the live place
    assert (isolatedCwd / "dist" / "place.rbxl").read_bytes().endswith(b"applied")
    assert result["artifact"]["bytes"] == len(operations.PLACE_FILE_MAGIC) + len(b"applied")


def test_no_build_keeps_the_place_file_it_was_given(stubbedBuild, isolatedCwd):
    """--no-build used to delete the very file it was told to reuse, so the
    flag worked exactly once."""
    supplied = isolatedCwd / "Place.rbxl"
    supplied.write_bytes(b"<roblox!supplied")

    operations.buildArtifact("prod", outPath="dist/place.rbxl", noBuild=True)

    assert supplied.exists()


def test_no_build_requires_the_place_file_to_exist(stubbedBuild):
    with pytest.raises(SGFLError) as excinfo:
        operations.buildArtifact("prod", outPath="dist/place.rbxl", noBuild=True)

    assert "--no-build" in excinfo.value.message


# --- retry classification --------------------------------------------------


@pytest.mark.parametrize(
    "method,status,allowUnsafe,expected",
    [
        ("POST", 429, False, True),  # rate limited: nothing was created
        ("GET", 503, False, True),
        ("GET", None, False, True),  # connection error
        ("POST", 500, False, False),  # may have created a version already
        ("POST", None, False, False),
        ("POST", 500, True, True),  # caller vouched it is replayable
        ("GET", 404, False, False),
        ("GET", 401, False, False),
    ],
)
def test_retry_classification(method, status, allowUnsafe, expected):
    assert cloud._isRetryable(method, status, allowUnsafe) is expected


def test_retry_delay_honors_retry_after():
    class FakeResponse:
        headers = {"Retry-After": "12"}

    assert cloud._retryDelay(FakeResponse(), 0) == 12


def test_retry_delay_backs_off_exponentially_and_caps():
    assert cloud._retryDelay(None, 0) == cloud.RETRY_BASE_DELAY_SECONDS
    assert cloud._retryDelay(None, 1) == cloud.RETRY_BASE_DELAY_SECONDS * 2
    assert cloud._retryDelay(None, 99) == cloud.RETRY_MAX_DELAY_SECONDS
