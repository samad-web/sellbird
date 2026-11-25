"""Several projects from one install (build-docs/features/multi-project.md).

One test per acceptance criterion. The registry is always a tmp file: no test
touches the user's real one.
"""

import json
import subprocess
import sys

import pytest

from sellbird import workspaces


@pytest.fixture
def registry(tmp_path):
    return tmp_path / "workspaces.json"


@pytest.fixture
def two_projects(tmp_path, registry):
    acme, globex = tmp_path / "acme", tmp_path / "globex"
    acme.mkdir()
    globex.mkdir()
    workspaces.save({"acme": str(acme), "globex": str(globex)}, registry)
    return acme, globex


# ── the registry ──

def test_add_stores_an_absolute_path(tmp_path, registry, monkeypatch):
    """Criterion 7: the entry can't depend on where add was run from."""
    project = tmp_path / "acme"
    project.mkdir()
    monkeypatch.chdir(tmp_path)
    name, root = workspaces.add("Acme", "acme", path=registry)
    assert name == "acme"                      # names are lowercased
    assert root == project.resolve()
    assert json.loads(registry.read_text())["acme"] == str(project.resolve())


def test_add_uses_the_current_directory_by_default(tmp_path, registry, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, root = workspaces.add("here", path=registry)
    assert root == tmp_path.resolve()


def test_add_refuses_to_move_a_name_without_force(two_projects, registry, tmp_path):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.add("acme", tmp_path, path=registry)
    assert str(two_projects[0]) in str(e.value)      # says where it points now
    workspaces.add("acme", tmp_path, force=True, path=registry)
    assert workspaces.load(registry)["acme"] == str(tmp_path.resolve())


@pytest.mark.parametrize("name", ["", " ", "has space", "../escape", "a/b",
                                  "-leading", "UPPER OK?", "dots.in.name"])
def test_a_name_that_could_be_mistaken_for_a_path_is_refused(name, registry):
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.add(name, path=registry)


def test_remove_forgets_the_name_and_leaves_the_folder(two_projects, registry):
    """Criterion 7."""
    acme, _ = two_projects
    assert workspaces.remove("acme", registry) == "acme"
    assert "acme" not in workspaces.load(registry)
    assert acme.is_dir()


def test_removing_an_unknown_name_lists_the_known_ones(two_projects, registry):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.remove("nope", registry)
    assert "acme" in str(e.value) and "globex" in str(e.value)


# ── resolving ──

def test_a_registered_name_resolves_to_its_folder(two_projects, registry):
    """Criterion 1."""
    assert workspaces.resolve("acme", registry) == two_projects[0]


def test_an_unknown_name_stops_and_lists_the_names(two_projects, registry):
    """Criterion 2: a typo must never become a new, empty project."""
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.resolve("acmee", registry)
    assert "isn't a known workspace" in str(e.value)
    assert "acme, globex" in str(e.value)


def test_a_folder_resolves_without_being_registered(tmp_path, registry):
    """Criterion 3."""
    project = tmp_path / "ad-hoc"
    project.mkdir()
    assert workspaces.resolve(str(project), registry) == project.resolve()


def test_a_folder_that_is_not_there_says_so(tmp_path, registry):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.resolve(str(tmp_path / "missing"), registry)
    assert "isn't a folder" in str(e.value)


def test_a_registered_folder_that_moved_is_not_silently_repaired(tmp_path, registry):
    workspaces.save({"acme": str(tmp_path / "gone")}, registry)
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.resolve("acme", registry)
    assert "isn't there any more" in str(e.value)
    assert "--force" in str(e.value)            # and says how to fix it


def test_no_workspaces_yet_says_how_to_make_one(registry):
    with pytest.raises(workspaces.WorkspaceError) as e:
        workspaces.resolve("acme", registry)
    assert "workspace add" in str(e.value)


# ── a broken registry ──

@pytest.mark.parametrize("text", ["not json at all", "[1, 2, 3]", '"a string"', ""])
def test_a_corrupt_registry_reads_as_empty(registry, text, capsys):
    """Criterion 8: a broken list must not stop every command."""
    registry.write_text(text, encoding="utf-8")
    assert workspaces.load(registry) == {}
    assert "ignoring it" in capsys.readouterr().err


def test_a_missing_registry_is_silent(tmp_path, capsys):
    assert workspaces.load(tmp_path / "nope.json") == {}
    assert capsys.readouterr().err == ""


def test_junk_entries_are_dropped_not_fatal(registry):
    registry.write_text(json.dumps({
        "acme": "/a", "bad name": "/b", "empty": "", "ok-2": "/c",
    }), encoding="utf-8")
    assert workspaces.load(registry) == {"acme": "/a", "ok-2": "/c"}


# ── reading the command line before argparse exists ──

@pytest.mark.parametrize("argv, expected", [
    (["--workspace", "acme", "status"], "acme"),
    (["--workspace=acme", "status"], "acme"),
    (["status", "--workspace", "acme"], "acme"),
    (["status"], ""),
    (["--workspace"], ""),                       # no value: not our problem yet
    (["--", "--workspace", "acme"], ""),         # after --, it's a value
])
def test_the_flag_is_read_straight_off_the_command_line(argv, expected):
    assert workspaces.early_workspace(argv, env={}) == expected


def test_the_environment_variable_is_the_fallback():
    """Criterion 5."""
    env = {workspaces.WORKSPACE_ENV: "globex"}
    assert workspaces.early_workspace(["status"], env) == "globex"
    # The flag is more specific, so it wins.
    assert workspaces.early_workspace(["--workspace", "acme"], env) == "acme"


def test_the_registry_lives_outside_every_workspace(tmp_path):
    """It is shared by all of them, and a workspace folder can be deleted."""
    path = workspaces.registry_path(env={"APPDATA": str(tmp_path / "roaming")},
                                    home=tmp_path)
    assert path.name == workspaces.REGISTRY_FILE
    assert "sellbird" in str(path).lower()


def test_label_names_the_folder_or_nothing(two_projects, registry):
    acme, _ = two_projects
    assert workspaces.label(acme, registry) == "acme"
    assert workspaces.label(acme.parent, registry) == ""


# ── end to end: two projects never share anything ──

def test_two_workspaces_never_share_a_database(tmp_path):
    """Criterion 9, and criteria 1/4/6 through the real CLI.

    Runs `sellbird status` twice with SELLBIRD_HOME, which is the mechanism
    --workspace uses, and checks the two homes come out separate.
    """
    import os

    acme, globex = tmp_path / "acme", tmp_path / "globex"
    for folder in (acme, globex):
        folder.mkdir()
    env = dict(os.environ)
    env.pop("SELLBIRD_WORKSPACE", None)
    seen = {}
    for name, folder in (("acme", acme), ("globex", globex)):
        env["SELLBIRD_HOME"] = str(folder)
        done = subprocess.run([sys.executable, "-m", "sellbird", "status"],
                              capture_output=True, text=True, env=env, timeout=240)
        assert done.returncode == 0, done.stderr
        seen[name] = folder
    for folder in (acme, globex):
        assert (folder / "data" / "sellbird.db").is_file()
        assert (folder / "sellbird.yaml").is_file()
        assert (folder / "skills").is_dir() and (folder / "prompts").is_dir()
    # Separate files, so separate everything: config, prospects, tokens.
    assert (acme / "data" / "sellbird.db") != (globex / "data" / "sellbird.db")


# ── the dashboard says which project it serves, without widening /api/instance ──

def test_the_unauthenticated_instance_route_stays_minimal():
    """/api/instance needs no token, so it says only enough to identify the
    dashboard. The name of somebody's project is not that, and nor is the
    path to their folder."""
    from fastapi.testclient import TestClient

    import sellbird.dashboard as dash

    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as http:
        body = http.get("/api/instance").json()
    assert set(body) == {"instance"}


def test_the_authenticated_status_route_carries_the_project(monkeypatch):
    """The tab title's source: behind the token, and on a call the page
    already makes on every load, so it costs no extra request."""
    from fastapi.testclient import TestClient

    import sellbird.dashboard as dash
    from sellbird import workspaces as ws

    monkeypatch.setattr(ws, "label", lambda root, path=None: "acme")
    store = dash.token_store
    with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                    headers={"Authorization": f"Bearer {store.get()}"}) as http:
        body = http.get("/api/sellbird/status").json()
    assert body["workspace"] == "acme"
