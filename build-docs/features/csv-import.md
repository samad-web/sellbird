# CSV import

## Purpose
Load a lead list someone already has (a spreadsheet, a researched sheet) into
Sellbird, so the Writer drafts for it and the Outbox holds every draft for
approval. Until now prospects came only from discovery and Scout.

## Architecture
- `sellbird import <file.csv> [--dry-run]` → `sellbird/importer.py`.
- `read_rows(path)`: UTF-8 (BOM tolerated) CSV. Headers are matched loosely
  (case, spaces, a few aliases): `company`/`business_name`/`name`, `email`,
  `website`/`domain`, `first_name`, `last_name`, `title`, `phone`,
  `city`/`location`, `industry`/`niche`, `notes`/`personalization_notes`,
  `source_url`.
- `import_rows(state, rows, env, dry_run)`: per row → validate → company
  (`state.add_company`, deduped by domain; no domain → `external_id`
  `import:<name>`) → prospect (`state.add_prospect`, status `new`).
- Email status comes from `verify_published_address` (MX + configured
  verifier), never from the file. Without a verifier key an address is
  `guess` and is never written for or sent — same rule as everywhere else.
- A row with no person's name is a company inbox (`source=company_inbox`,
  title "General inbox"), like Scout's.
- `notes` → `personalization_notes`: the Writer reads it as a fact about the
  prospect (fenced as untrusted, like all prospect data).

## Impact map
- `state.add_company` / `add_prospect` / `prospect_exists`: used as is.
- Writer: picks imported prospects up like any `new` prospect with a
  verified/risky address. Sender/gate/Outbox unchanged; approval still
  required; suppressions and the daily cap still apply at send.
- CLI: one new subcommand. No schema change, no migration.

## Edge cases
- Missing file, empty file, no `email` or `company` column → one clear error,
  nothing written.
- Row without email, with a malformed email, without company, or a named
  person without a title → skipped, reported with its row number.
- Same email twice in the file, or already on file → skipped.
- More than 2,000 rows → refused (every row costs a verification lookup).
- Long cells are cut (notes 1,000 chars, others 300).
- `--dry-run`: validates and reports, no verification calls, no writes.

## Acceptance criteria
1. A valid file imports companies + prospects with status `new`.
2. Email status is the verifier's verdict, not anything in the file.
3. Rows are deduped against the file and the database.
4. Bad rows are skipped with a reason; good rows still import.
5. A free-mail address (gmail.com…) never becomes the company's domain.
6. `--dry-run` writes nothing and verifies nothing.
7. Missing required columns → error, nothing written.
