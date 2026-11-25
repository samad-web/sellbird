"""Named projects: run Sellbird for several businesses from one install.

The isolation itself is older than this module: ``paths.resolve_home`` already
puts every file one project needs (config, .env, prompts, skills, database,
OAuth and dashboard tokens) under ``SELLBIRD_HOME``. This is the address book
in front of it, so a project is ``--workspace acme`` instead of a path typed
from memory -- and so every command can say which one it just acted on, which
matters more: `outbox --approve-all` in the wrong terminal approves the wrong
company's email, and nothing in the output would have told you.

The registry lives in the per-user app folder, never inside a workspace: it is
shared by all of them, and a workspace folder can be deleted.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from sellbird import paths

REGISTRY_FILE = "workspaces.json"
# A name, so it can never be mistaken for a path: no slash, no dot, no space.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
WORKSPACE_ENV = "SELLBIRD_WORKSPACE"


class WorkspaceError(Exception):
    """Said to the user as it is: these messages are the whole UI."""


def registry_path(env=None, home: Path | None = None) -> Path:
    """Where the address book is. Outside every workspace, by design."""
    env = os.environ if env is None else env
    folder = paths.user_home_default(
        sys.platform, env, Path.home() if home is None else home)
    return Path(folder) / REGISTRY_FILE


def load(path: Path | None = None) -> dict[str, str]:
    """{name: path}. A missing, unreadable or corrupt file reads as empty:
    a broken registry must not stop every command."""
    path = registry_path() if path is None else Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        print(f"  Note: {path} isn't readable as a workspace list; ignoring it.",
              file=sys.stderr)
        return {}
    if not isinstance(raw, dict):
        print(f"  Note: {path} isn't a workspace list; ignoring it.", file=sys.stderr)
        return {}
    return {str(k).strip().lower(): str(v) for k, v in raw.items()
            if NAME_RE.match(str(k).strip().lower()) and str(v).strip()}


def save(workspaces: dict[str, str], path: Path | None = None) -> Path:
    """Write the address book. Replaced, never appended to, so a failed write
    can't leave half a list."""
    path = registry_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(workspaces, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)
    return path


def add(name: str, folder: Path | str | None = None, *, force: bool = False,
        path: Path | None = None) -> tuple[str, Path]:
    """Register a project. The folder is stored resolved, so the entry doesn't
    depend on where this was run from."""
    key = (name or "").strip().lower()
    if not NAME_RE.match(key):
        raise WorkspaceError(
            f"'{name}' isn't a usable name. Use letters, digits, '-' and '_', "
            "starting with a letter or digit.")
    root = Path(folder or Path.cwd()).expanduser().resolve()
    workspaces = load(path)
    if key in workspaces and not force:
        raise WorkspaceError(
            f"'{key}' is already {workspaces[key]}. Pass --force to point it "
            "somewhere else.")
    workspaces[key] = str(root)
    save(workspaces, path)
    return key, root


def remove(name: str, path: Path | None = None) -> str:
    """Forget a project. The folder and everything in it is left alone."""
    key = (name or "").strip().lower()
    workspaces = load(path)
    if key not in workspaces:
        raise WorkspaceError(f"'{name}' isn't a known workspace.{_known(workspaces)}")
    del workspaces[key]
    save(workspaces, path)
    return key


def resolve(wanted: str, path: Path | None = None) -> Path:
    """A registered name or a folder, as a path.

    A name that isn't registered is an error naming the ones that are: a
    typo must never silently become a new, empty project, because an empty
    project looks like one that has lost its data.
    """
    wanted = (wanted or "").strip()
    if not wanted:
        raise WorkspaceError("No workspace given.")
    workspaces = load(path)
    key = wanted.lower()
    if key in workspaces:
        root = Path(workspaces[key]).expanduser()
        if not root.is_dir():
            raise WorkspaceError(
                f"'{key}' points at {root}, which isn't there any more. "
                f"Move it back, or run: sellbird workspace add {key} <path> --force")
        return root
    looks_like_a_name = bool(NAME_RE.match(key))
    root = Path(wanted).expanduser()
    if root.is_dir():
        return root.resolve()
    if looks_like_a_name:
        raise WorkspaceError(f"'{wanted}' isn't a known workspace.{_known(workspaces)}")
    raise WorkspaceError(f"{root} isn't a folder.")


def _known(workspaces: dict[str, str]) -> str:
    if not workspaces:
        return ("\n  You have no workspaces yet. Create one with: "
                "sellbird workspace add <name> <path>")
    return "\n  Known: " + ", ".join(sorted(workspaces))


def early_workspace(argv: list[str], env=None) -> str:
    """The workspace asked for, read straight off the command line.

    Called before ``sellbird.paths`` is imported, because that module decides
    the home folder once, at import. The flag wins over the environment
    variable: it is the more specific of the two.
    """
    env = os.environ if env is None else env
    for i, arg in enumerate(argv):
        if arg == "--workspace" and i + 1 < len(argv):
            return argv[i + 1].strip()
        if arg.startswith("--workspace="):
            return arg.split("=", 1)[1].strip()
        if arg == "--":           # everything after this is a value, not a flag
            break
    return (env.get(WORKSPACE_ENV) or "").strip()


def label(root: Path | str, path: Path | None = None) -> str:
    """The name this folder is registered under, else "" -- for the one line
    a command prints to say which project it acted on."""
    try:
        resolved = Path(root).expanduser().resolve()
    except OSError:
        return ""
    for name, folder in sorted(load(path).items()):
        try:
            if Path(folder).expanduser().resolve() == resolved:
                return name
        except OSError:
            continue
    return ""
