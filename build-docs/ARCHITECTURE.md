# Architecture

The system design already lives in the repo and is kept current there. This file points
at it rather than duplicating it (duplicated architecture docs drift).

- **Overview and data flow:** `docs/how-it-works.md`, and "How Sellbird Works" in `CLAUDE.md`
- **Approval and sending ladder:** `docs/approval-and-sending.md`
- **Signals / observation model:** `docs/signals-and-pipeline.md`
- **Config reference:** `docs/configuration.md`
- **Skills and agents:** `docs/skills-and-agents.md`
- **Dashboard HTTP surface and auth:** `sellbird/dashboard_auth.py` (module docstring),
  `sellbird/agent_policy.py` (what the coding agent's token may call)

## Module boundaries worth knowing before changing anything

- `sellbird/main.py` — the heartbeat. Decides, runs agents in parallel, sends in a
  second phase only after every reply-reading task succeeded.
- `sellbird/agents/` — Scout (find), Writer (write), Sender (send), Handler (replies),
  Analyst (idle-cycle stats). Agents never talk to each other; they meet in the database.
- `sellbird/state.py` — the only module that writes SQLite. Facts are rows in
  `observations`, never new columns.
- `sellbird/gate.py` — deterministic checks every outgoing message passes. No model call.
  This is the last line before a message leaves; nothing bypasses it.
- `sellbird/integrations/mail_provider.py` — the `MailProvider` interface. A new sending
  channel is a class behind it, not a new pipeline. (The WhatsApp channel uses this.)

## Planned change

`build-docs/features/whatsapp-channel.md` adds a `channel` dimension to the outbox and a
second provider behind `MailProvider`. No new agent, no second pipeline.
