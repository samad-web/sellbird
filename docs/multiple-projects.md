# Several projects from one install

One Sellbird install can run outreach for several businesses. Each project
keeps its own config, mailbox, prospects, database, OAuth tokens and dashboard
token. Nothing is shared.

## Add a project

```bash
sellbird workspace add acme   ~/sellbird/acme
sellbird workspace add globex ~/sellbird/globex
```

The folder doesn't need to exist yet — Sellbird fills a new one with the
default `sellbird.yaml`, `prompts/` and `skills/` the first time you run a
command against it.

Then set each one up as usual:

```bash
sellbird --workspace acme setup
sellbird --workspace acme train https://acme.example.com
```

## Work in one

Two ways, and the flag always wins:

```bash
sellbird --workspace acme status          # one command
export SELLBIRD_WORKSPACE=acme            # this terminal, until you change it
sellbird status
```

Every command prints the project it acted on:

```
  [workspace: acme]
```

That line goes to stderr, so piping a CSV or a protocol is unaffected. A
single-project install prints nothing extra and behaves exactly as before.

## See what you have

```bash
sellbird workspace            # the list; * is the one this terminal is on
sellbird workspace which      # just this one
sellbird workspace remove old # forget it; the folder and data stay
```

## Two dashboards at once

Each project's dashboard needs its own port, and has its own unlock token:

```bash
sellbird --workspace acme   dashboard                 # 127.0.0.1:5555
sellbird --workspace globex dashboard --port 5556
```

The browser tab is titled with the project's name, so two open dashboards are
easy to tell apart — worth knowing, because the Outbox is one click from
sending.

## Two heartbeats at once

Run one `sellbird run` per project, in its own terminal:

```bash
sellbird --workspace acme   run
sellbird --workspace globex run
```

They are separate processes with separate databases and separate daily limits.
Both draw on the same Claude subscription, so `usage.max_daily_claude_percent`
in each config is a share of the *same* quota: two projects at 80% will
exhaust it. Set each to a figure that adds up to what you want to spend.

## A folder, without registering it

```bash
sellbird --workspace ./some/folder status
```

Useful once; a name is better for anything you'll do twice. A `--workspace`
value that is neither a known name nor an existing folder stops the command
and lists the names it knows — a typo never silently becomes a new, empty
project, because an empty project looks exactly like one that lost its data.

## Where the list lives

`workspaces.json`, in your per-user app folder (`%APPDATA%\sellbird` on
Windows, `~/Library/Application Support/sellbird` on macOS,
`$XDG_DATA_HOME/sellbird` on Linux) — outside every project, since they all
share it and any one project's folder can be deleted. If it ever gets
corrupted, Sellbird says so once and carries on with no projects registered,
rather than refusing to run.

## How it works underneath

`--workspace` resolves the name and sets `SELLBIRD_HOME`, which is how
Sellbird has always chosen its home folder (`sellbird/paths.py`). So the flag,
the environment variable and Docker's `/app` mount all take the same path
through the code. You can keep using `SELLBIRD_HOME` directly; it is honoured
whenever no workspace is named.
