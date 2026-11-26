# Signature footer

## Purpose
With a logo set, the HTML email was Sellbird's text, the opt-out line, then the
logo: the footer sat above the brand mark and looked like body text. Operators
want a polished, branded look without a marketing template.

## Decision
Personal-letter layout, not a newsletter template (header banner, card, button,
social icons). Templates get cold mail filed under Promotions or Spam, and social
links fail the `link_domains` check. Operator chose this after being told.

## Architecture
- `Signature` gains `accent` (#RRGGBB) and `footer` (= `compliance_footer_lines`).
- `render_html`: if the body ends with the footer lines, they move below the logo,
  under a 1px `accent` rule, 12px grey. Text unchanged; only placement.
- `SignatureConfig.accent`, validated as a hex colour (it lands in a style attribute).

## Impact map
- Gmail / SMTP (`set_body`) and Microsoft (`render_html`): HTML only; plain text unchanged.
- Sender `_with_footer` / pre-send gate: unchanged; they see the same text.
- Replies: no footer, so no rule.

## Edge cases
- Footer text appearing mid-body is not moved (must be the body's end).
- No postal address: footer is the opt-out line alone.
- Bad accent (`red`, `#fff;x:y`) refused at config load.

## Acceptance criteria
1. Cold email: logo, then rule, then footer; footer appears once.
2. Reply: no rule.
3. Footer text not at the end stays in place.
4. Non-hex accent is refused; blank falls back to the default.
