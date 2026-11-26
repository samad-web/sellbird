# Branded email style

## Purpose
- Some users want cold email to look like their brand's own mail (header, button, footer band), not a plain letter.
- Opt-in: `signature.style: branded`. Default `letter` is unchanged.

## Decision (2026-11-26)
- User chose the full branded template after being told it moves cold email towards Promotions or Spam and lowers replies.
- Kept opt-in so the safer default stays for everyone else; recorded in MEMORY.md.

## Architecture
- `SignatureConfig.style` (`letter` | `branded`) and `SignatureConfig.button` (label, blank = no button).
- The button opens `offer.booking_url` (one booking link, not a second copy in the signature).
- `render_html` picks the layout. Branded, built from tables so Outlook keeps it:
  - grey canvas, white 600px card;
  - 4px `accent` strip;
  - header: logo + `persona.company`;
  - the letter;
  - the button in `accent`, on cold emails only;
  - dark footer band: company, then the compliance lines.
- Plain-text part and pre-send gate are unchanged: they see Sellbird's words only.

## Impact map
- `sellbird/config.py`: two fields, validators.
- `sellbird/integrations/signature.py`: `Signature` fields, `load_signature`, `render_html`.
- Microsoft (`render_html`), Gmail/SMTP (`set_body`): no change; they call the same functions.
- Sender outbox hold (`signature_problem`): a button with no usable booking URL holds the outbox, like an unusable logo.

## Edge cases
- Button set, `offer.booking_url` blank or not https → `SignatureError`, outbox held, reason shown on Today.
- Reply (no compliance footer at the end) → no button; footer band shows the company only.
- Blank company → header shows the logo alone, no company line in the band.
- Label, company and footer text are HTML-escaped.
- Logo-less config → no signature at all (plain text), whatever the style.

## Acceptance criteria
- AC1: `style: branded` renders the strip, header with company, letter, dark band with the footer lines, in that order.
- AC2: the button appears on a cold email with the booking URL and label, and not on a reply.
- AC3: a button with no https booking URL is a `SignatureError`.
- AC4: `style` other than letter/branded is refused by config; default stays `letter` with today's HTML.
