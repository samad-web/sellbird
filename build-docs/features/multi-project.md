# Multiple projects, one install

## Purpose

Run Sellbird for several businesses from one install, each with its own
product, ICP, mailbox, prospects and database, and switch between them without
retyping a path or risking sending from the wrong one.

## What already exists (read off the code, 2026-11-25)

**It works today.** `sellbird/paths.py` resolves the home folder in this order:
`SELLBIRD_HOME`, then a source checkout, then a per-user folder. So:

```bash
SELLBIRD_HOME=~/sellbird/acme    sellbird status
SELLBIRD_HOME=~/sellbird/globex  sellbird status
```

Verified end to end: each folder got its own `sellbird.yaml`, `prompts/`,
`skills/` and `data/sellbird.db`, seeded from the checkout's defaults by
`home.seed_home`. Nothing leaked between them.

Underneath, `workspace_home.Home` already carries every path one workspace
needs (db, .env, prompts, skills, OAuth tokens, dashboard token) and lives in a
`ContextVar`, so one process can serve several. `StateManager()` reads
`current_home().db_path`. The dashboard already runs per-home
(`home_middleware`). The MCP server already takes an optional `workspace`
argument on every tool — but `resolve` is a hook a hosted server supplies;
there is no local registry behind it.

So this feature adds **no new isolation**. It adds the ergonomics that make the
existing isolation safe to use.

## What is actually missing

1. You must type a full path in an environment variable every time.
2. **No command says which project it just acted on.** This is the real risk:
   `sellbird outbox --approve-all` in the wrong terminal approves the wrong
   company's emails. Nothing in the output would tell you.
3. No way to list the projects you have.
4. Each project's dashboard needs its own `--port`, which you have to remember.

## Architecture

- **`sellbird/workspaces.py`** — a registry: `{name: path}` in
  `workspaces.json`, in the per-user app folder (`paths.user_home_default`),
  deliberately *not* inside any workspace, since it is shared by all of them.
  Pure functions over a dict plus one atomic write. A name is
  `[a-z0-9][a-z0-9_-]*`, lowercased, so it can't be confused with a path.
- **`--workspace NAME|PATH`** on the top-level CLI, plus `SELLBIRD_WORKSPACE`
  for a terminal that stays in one project. Resolved in `main()` **before
  `sellbird.paths` is imported**, and applied by setting `SELLBIRD_HOME` — the
  one path already proven to work, rather than a second mechanism beside it.
  `cli.py` has no top-level Sellbird imports, so this is possible; a pre-parse
  of `sys.argv` reads the value without consuming it.
- **`sellbird workspace`** — `list` (the default), `add <name> [path]`,
  `remove <name>`, `which`.
- **One line of output** naming the workspace, on every command, whenever it is
  not the default home. Printed to stderr so a command whose stdout is a CSV or
  a protocol is unaffected.

## Impact map

| Touched | What could break |
|---|---|
| `cli.py` `main()` | the pre-parse runs before everything; a bug here breaks every command. Fails open: anything it can't resolve is reported and the default home is used |
| `SELLBIRD_HOME` | set by us when `--workspace` is given. An explicit `SELLBIRD_HOME` with no `--workspace` keeps winning, so Docker is unaffected |
| `paths.resolve_home` | unchanged |
| `home.seed_home` | already runs per command; a new workspace folder is seeded on first use |
| the dashboard | unchanged. Each workspace keeps its own token file, so two dashboards on two ports stay separately locked |
| nothing else | no module changes: the isolation already exists |

## Edge cases

- `--workspace` naming neither a registered name nor an existing folder → the
  command stops with the known names listed. Guessing a path would be worse.
- A registered path that has been deleted or moved → same message; the registry
  is not silently repaired.
- `--workspace` pointing at a path directly (not a name) → used as-is, no
  registry entry created.
- Both `SELLBIRD_HOME` and `--workspace` → the flag wins, and says so.
- `add` with no path → the current directory.
- `add` of a name that exists → refuses unless `--force`, naming the old path.
- A registry file that is corrupt, unreadable, or not a JSON object → treated as
  empty, with one warning. A broken registry must not stop every command.
- Two workspaces, one dashboard port → the second fails to bind; the message
  says to pass `--port`.
- A relative path in the registry → stored resolved, so it doesn't depend on
  where you ran `add` from.

## Acceptance criteria

1. `sellbird --workspace acme status` reads acme's database, with `acme`
   registered to a folder.
2. The same command against an unregistered name stops and lists the names.
3. `--workspace ./some/dir` works with no registry entry.
4. Every command names the workspace it acted on, on stderr, when it isn't the
   default; and says nothing extra when it is.
5. `SELLBIRD_WORKSPACE=acme sellbird status` behaves as the flag does; the flag
   wins when both are set.
6. An explicit `SELLBIRD_HOME` is still honoured when no workspace is named.
7. `workspace add` stores an absolute path; `list` shows each name, path, and
   whether the folder exists; `remove` forgets one without touching the folder.
8. A corrupt registry file does not stop a command.
9. Two workspaces never share a database, config, prompts, skills or dashboard
   token.

## Out of scope

- One process serving several projects at once. `Home` supports it and the
  hosted server uses it; a self-hosted user runs `sellbird run` per project.
- Teaching the local MCP server to reach another project's dashboard. That
  needs a port per workspace in the registry and a client factory that reads
  it; worth doing only once someone runs two at once.
