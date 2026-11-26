"""A logo under every email the native providers send.

Off by default: email goes out as plain text. With
``channels.email.signature.logo`` set, Gmail, Microsoft 365 and SMTP send
each email as HTML, with the logo embedded in the message itself (an inline
``cid:`` image), so it shows without the recipient allowing remote images.
Gmail and SMTP also carry the same plain text as the HTML's alternative.

The words are the ones Sellbird wrote, unchanged: the HTML is that text,
escaped, with its paragraphs and line breaks kept and its web addresses made
clickable. The pre-send gate checks that text, so the only link the HTML adds
is the logo's own ``link``, which you set. A cold email's compliance footer
(postal address, opt-out line) is set apart under a thin rule in the
``accent`` colour, in small grey type; the words stay the same.
``style: branded`` swaps the letter for a designed layout (header, card,
booking button on cold email, dark footer band); same words, same text part.

A logo that's missing, too big or not an image never lets an email go out
without it: the sender holds the outbox (``signature_problem``) and the
providers refuse to send (``SignatureError``).
"""

import hashlib
import html
import re
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from sellbird.config import compliance_footer_lines
from sellbird.paths import PROJECT_ROOT

# Bigger images hurt deliverability, and a logo never needs more.
MAX_LOGO_BYTES = 100_000
MAX_LOGO_PIXELS = 20_000

_URL = re.compile(r"https?://[!#-;=?-~]+")   # printable ASCII except space, " < >
_URL_TRAILING = ".,;:!?)]}'"


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class Signature:
    data: bytes
    mime: str             # image/png, image/jpeg or image/gif
    filename: str
    content_id: str       # the src="cid:..." the HTML points at
    width: int
    height: int
    alt: str
    link: str
    direction: str        # rtl, ltr or auto
    accent: str = "#e5e7eb"          # the rule above the footer
    footer: tuple[str, ...] = ()     # compliance lines a cold email ends with
    style: str = "letter"            # or "branded"
    company: str = ""
    button: str = ""                 # branded: label of the booking button
    button_url: str = ""


def _settings(config):
    email = getattr(getattr(config, "channels", None), "email", None)
    return getattr(email, "signature", None)


def load_signature(config) -> Signature | None:
    """The configured signature, or None when there isn't one. Raises
    SignatureError when there is one but its logo can't be used."""
    settings = _settings(config)
    logo = (getattr(settings, "logo", "") or "").strip()
    if not logo:
        return None
    try:
        path = Path(logo).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        size = path.stat().st_size
        if size > MAX_LOGO_BYTES:
            raise SignatureError(
                f"logo {path.name} is {size // 1000} KB; keep it under "
                f"{MAX_LOGO_BYTES // 1000} KB")
        data = path.read_bytes()
    except FileNotFoundError:
        raise SignatureError(f"logo file not found: {path}") from None
    except (OSError, RuntimeError, ValueError) as e:   # ~nouser, a NUL in the path
        raise SignatureError(f"logo file can't be read: {logo} ({e})") from None
    mime = _image_type(data)
    if not mime:
        raise SignatureError(f"logo {path.name} isn't a PNG, JPEG or GIF image")
    width = int(getattr(settings, "width", 120) or 120)
    size_px = _image_size(data, mime)
    # A header with no usable size is a corrupt file, which would show as a
    # broken image in every email.
    if not size_px or not all(0 < n <= MAX_LOGO_PIXELS for n in size_px):
        raise SignatureError(f"logo {path.name} is a damaged image")
    height = round(width * size_px[1] / size_px[0])
    company = (getattr(getattr(config, "persona", None), "company", "") or "").strip()
    style = getattr(settings, "style", "letter") or "letter"
    button = (getattr(settings, "button", "") or "").strip() if style == "branded" else ""
    button_url = ""
    if button:
        button_url = (getattr(getattr(config, "offer", None), "booking_url", "") or "").strip()
        if not re.match(r"https://[^\s\"<>]+$", button_url):
            raise SignatureError(
                "signature.button needs offer.booking_url set to an https:// address")
    return Signature(
        data=data,
        mime=mime,
        filename=path.name,
        content_id=f"logo-{hashlib.sha256(data).hexdigest()[:16]}",
        width=width,
        height=height,
        alt=(getattr(settings, "alt", "") or company or "logo").strip(),
        link=(getattr(settings, "link", "") or "").strip(),
        direction=(getattr(settings, "direction", "") or "auto").strip().lower(),
        accent=(getattr(settings, "accent", "") or "#e5e7eb").strip(),
        footer=tuple(compliance_footer_lines(config)),
        style=style,
        company=company,
        button=button,
        button_url=button_url,
    )


def signature_problem(config) -> str:
    """Why the configured signature can't be used, or "" when it can (or
    when there's none)."""
    try:
        load_signature(config)
    except SignatureError as e:
        return str(e)
    return ""


def render_html(body: str, sig: Signature) -> str:
    """The email as HTML: Sellbird's text, the logo, then the cold-email
    footer under a rule. Replies have no footer, so they end at the logo."""
    body = body.replace("\r\n", "\n").strip()
    footer = "\n".join(sig.footer)
    if footer and body.endswith("\n" + footer):
        body = body[: -len(footer)].rstrip()
    else:
        footer = ""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", body) if p.strip()]
    text = "\n".join(
        f'<p style="margin:0 0 1em 0">{_paragraph(p)}</p>' for p in paragraphs
    )
    height = f' height="{sig.height}"'
    img = (
        f'<img src="cid:{sig.content_id}" width="{sig.width}"{height} '
        f'alt="{html.escape(sig.alt)}" '
        f'style="display:block;border:0;width:{sig.width}px;height:auto">'
    )
    if sig.link:
        img = f'<a href="{html.escape(sig.link)}">{img}</a>'
    if sig.direction == "auto":
        direction = 'dir="auto"'
    else:
        # Outlook's desktop renderer ignores dir="auto" and reads the style.
        align = "right" if sig.direction == "rtl" else "left"
        direction = f'dir="{sig.direction}" style="direction:{sig.direction};text-align:{align}"'
    if sig.style == "branded":
        return _branded(text, img, footer, sig, direction)
    foot = (
        f'<div style="margin-top:28px;padding-top:14px;border-top:1px solid {sig.accent};'
        f'font-size:12px;line-height:1.6;color:#64748b">{_paragraph(footer)}</div>\n'
        if footer else ""
    )
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"></head><body>\n'
        f'<div {direction}>\n'
        '<div style="max-width:560px;font-family:-apple-system,\'Segoe UI\',Helvetica,Arial,'
        'sans-serif;font-size:15px;line-height:1.6;color:#0f172a">\n'
        f"{text}\n"
        f'<div style="margin-top:20px">{img}</div>\n'
        f"{foot}"
        "</div>\n</div>\n</body></html>\n"
    )


_FONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"


def _branded(text: str, img: str, footer: str, sig: Signature, direction: str) -> str:
    """The branded layout, in tables so Outlook keeps it: accent strip,
    logo and company, the letter, a booking button (cold email only), and a
    dark band with the company and the compliance lines."""
    a, company = sig.accent, html.escape(sig.company, quote=False)
    name = (f'<td style="vertical-align:middle;padding-left:12px;font-size:17px;'
            f'font-weight:600;color:#0f172a">{company}</td>' if company else "")
    button = ""
    if footer and sig.button:   # only a cold email ends with the footer
        button = (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:8px 0 8px"><tr><td bgcolor="{a}" style="border-radius:8px;background:{a}">'
            f'<a href="{html.escape(sig.button_url)}" style="display:inline-block;padding:12px 26px;'
            'font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;border-radius:8px">'
            f'{html.escape(sig.button, quote=False)}</a></td></tr></table>\n'
        )
    band = (f'<div style="color:#ffffff;font-size:13px;font-weight:600;margin-bottom:6px">{company}</div>'
            if company else "") + (_paragraph(footer) if footer else "")
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>\n'
        '<body style="margin:0;padding:0;background:#f1f5f9">\n'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'bgcolor="#f1f5f9" style="background:#f1f5f9"><tr><td align="center" style="padding:32px 12px">\n'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;font-family:{_FONT}">\n'
        f'<tr><td height="4" bgcolor="{a}" style="height:4px;line-height:4px;font-size:0;background:{a}">&nbsp;</td></tr>\n'
        '<tr><td style="padding:22px 36px;border-bottom:1px solid #e5e7eb">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td style="vertical-align:middle">{img}</td>{name}</tr></table></td></tr>\n'
        '<tr><td style="padding:32px 36px 20px;font-size:15px;line-height:1.65;color:#0f172a">\n'
        f'<div {direction}>\n{text}\n{button}</div></td></tr>\n'
        + (f'<tr><td bgcolor="#0f172a" style="background:#0f172a;padding:24px 36px;font-size:12px;'
           f'line-height:1.7;color:#94a3b8">{band}</td></tr>\n' if band else "")
        + '</table>\n</td></tr></table>\n</body></html>\n'
    )


def set_body(msg: EmailMessage, body: str, sig: Signature | None):
    """Plain text alone, or with a signature: plain text plus an HTML
    alternative that carries the logo as an inline image."""
    msg.set_content(body)
    if sig is None:
        return
    msg.add_alternative(render_html(body, sig), subtype="html")
    maintype, subtype = sig.mime.split("/")
    msg.get_payload()[-1].add_related(
        sig.data, maintype=maintype, subtype=subtype,
        cid=f"<{sig.content_id}>", filename=sig.filename, disposition="inline",
    )


def _paragraph(text: str) -> str:
    out, pos = [], 0
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(_URL_TRAILING)
        if len(url) <= len("https://"):
            continue
        out.append(html.escape(text[pos:m.start()], quote=False))
        out.append(f'<a href="{html.escape(url)}">{html.escape(url, quote=False)}</a>')
        pos = m.start() + len(url)
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out).replace("\n", "<br>\n")


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return ""


def _image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    """(width, height) from the image's header, or None."""
    if mime == "image/png" and len(data) >= 24 and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if mime == "image/gif" and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if mime == "image/jpeg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                return None
            marker = data[i + 1]
            length = int.from_bytes(data[i + 2:i + 4], "big")
            # Start-of-frame markers carry the size; C4, C8 and CC don't.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
            i += 2 + length
    return None
