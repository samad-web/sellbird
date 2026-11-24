"""Deterministic pre-send gate — the last line before an email leaves.

LLM instructions are a floor, not a guarantee. Every outgoing email passes
these non-LLM checks at send time; a failure blocks the send and records
why, so a bad generation can never reach a prospect. This is the pattern
the surviving commercial AI-SDR vendors converged on after the 2025
autonomous-agent flameouts: constraints in the prompt, verification in
code.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from sellbird.phone import is_e164, looks_like_phone
from sellbird.phone import normalize as normalize_phone
from sellbird.phone import same_number

# Spam-filter and AI-tell phrases that must never appear in outgoing mail.
# Mirrors prompts/writer.md; enforced here because prompts can be ignored.
BANNED_PHRASES = (
    "act now", "buy now", "click here", "click below", "limited time",
    "limited offer", "exclusive deal", "special offer", "risk-free",
    "no obligation", "100% free", "make money", "double your",
    "guaranteed results", "winner", "congratulations",
    "i hope this finds you well", "hope this email finds you well",
    "i hope this email finds you", "picture this", "what if i told you",
    "in today's fast-paced world", "game-changer", "game changer",
    "cutting-edge solution", "revolutionize your", "unlock your",
    "supercharge your", "as an ai", "i'm an ai", "i am an ai",
    "as a language model",
)

MAX_SUBJECT_LEN = 90
# WhatsApp: Meta allows 4096 characters. This is the length past which a
# cold message reads as a wall of text and gets the sender blocked.
MAX_WHATSAPP_CHARS = 700
MAX_BODY_WORDS = 220        # hard ceiling; sequences aim far lower
MAX_LINKS = 1

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MERGE_TAG_RE = re.compile(r"\{\{\s*\w+\s*\}\}|\{\s*\w+\s*\}(?!\})")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# Email statuses a cold email may go to. "risky" is a catch-all domain: it
# accepts everything, so a wrong guess still bounces, which is why it is
# opt-in via channels.email.send_to_risky. One definition, because the gate
# and the Sender must never disagree about what may go out.
VERIFIED_ONLY = frozenset({"verified"})
DELIVERABLE_STATUSES = frozenset({"verified", "risky"})


def sendable_email_statuses(allow_risky: bool) -> frozenset:
    """The email statuses that may be sent to under this setting."""
    return DELIVERABLE_STATUSES if allow_risky else VERIFIED_ONLY


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


# Characters that survive .strip() and make an entry quietly match nothing.
_INVISIBLE_RE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")
_DNC_DOMAIN_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_DNC_LOCAL_RE = re.compile(r"^[a-z0-9._%+'-]+$")


def _clean_text(raw) -> str:
    text = unicodedata.normalize("NFKC", str(raw or ""))
    return _INVISIBLE_RE.sub("", text).replace("\u3002", ".")


def _dnc_domain(value: str) -> str:
    domain = value.split(":", 1)[0].strip().strip(".")
    if domain.startswith("www."):
        domain = domain[4:]
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return domain if _DNC_DOMAIN_RE.match(domain) else ""


def do_not_contact_entry(raw) -> str:
    """One do-not-contact entry as an address, a bare domain or an E.164
    phone number, or "" when it names none of those.

    People paste what the browser shows, so "https://www.acme.example/about",
    "mailto:jane@acme.example", "*.acme.example", "*@acme.example" and
    "acme.example:443" all reduce to what they name. Left alone, each would
    quietly match nothing. An international domain becomes its ASCII form,
    the one recipient addresses carry.

    A phone number must be written in international form ("+441632960001",
    spaces and brackets allowed). A local number is refused rather than
    guessed: "020 7946 0018" names a different line in every country, and an
    entry that blocks the wrong person is worse than one that errors.
    """
    entry = _clean_text(raw).strip().lower()
    entry = re.sub(r"^(?:tel|callto|whatsapp|wa):\s*", "", entry)
    if entry.startswith("+"):
        # The leading "+" is what makes it a phone number and not a domain:
        # no address or domain starts with one, and "123.456" would otherwise
        # be readable as either.
        return normalize_phone(entry)
    entry = re.sub(r"^(?:[a-z][a-z0-9+.-]*://|mailto:)", "", entry)
    entry = re.split(r"[/?#]", entry, maxsplit=1)[0]
    entry = re.sub(r"^(?:\*@|\*\.|@)", "", entry)
    if "@" in entry:
        local, _, domain = entry.rpartition("@")
        domain = _dnc_domain(domain)
        if not domain or not _DNC_LOCAL_RE.match(local):
            return ""
        return f"{local}@{domain}"
    return _dnc_domain(entry)


# A phone number in international form, which may carry the spaces and
# brackets people write it with — so it is lifted out before the separator
# split tears it into three "entries". The lookbehind keeps it off the
# plus-tag in an address like "jane+1234567@acme.example".
_PHONE_ENTRY_RE = re.compile(r"(?<![\w@.])\+[\d\s()./\-]{6,25}\d(?![\w@])")


def do_not_contact_entries(raw) -> list[str]:
    """Every entry one configured string holds. Commas, semicolons and spaces
    separate entries; "Jane Doe <jane@acme.example>" is that address, and a
    "+" number keeps its own spaces. Raises ValueError for a piece that is
    none of an address, a domain or an international phone number: an entry
    that silently matches nothing leaves the user believing a company is
    protected when it isn't."""
    text = _clean_text(raw)
    angled = re.findall(r"<([^<>\s]+@[^<>\s]+)>", text)
    if angled:
        parts = angled
    else:
        phones: list[str] = []

        def _lift(match):
            phones.append(match.group(0))
            return " "

        rest = _PHONE_ENTRY_RE.sub(_lift, text)
        parts = phones + re.split(r"[\s,;]+", rest)
    found = []
    for part in parts:
        if not part.strip():
            continue
        entry = do_not_contact_entry(part)
        if not entry:
            raise ValueError(
                f"'{part.strip()}' is not an email address, a domain, or a phone "
                "number in international form (+441632960001)"
            )
        found.append(entry)
    return found


def do_not_contact_match(to_email: str, entries) -> str:
    """The do-not-contact entry ``to_email`` matches, or "".

    An entry with an "@" is one address, plus-tags included ("jane@" also
    covers "jane+news@"). Anything else is a domain and covers its
    subdomains, matched on a label boundary so "acme.example" blocks
    "mail.acme.example" without also blocking "notacme.example" — a suffix
    test without that boundary is the classic way a blocklist overreaches.

    A recipient that is a phone number (the WhatsApp channel) is matched
    against the "+" entries only, on the subscriber's digits. Address and
    domain entries say nothing about a phone number, and vice versa: the
    same person on the list twice is the user's business, not a bug.

    Entries are expected already normalised by ComplianceConfig; this
    normalises anyway so a hand-built tuple behaves the same way.
    """
    address = (to_email or "").strip().lower()
    if not entries:
        return ""
    if "@" not in address and looks_like_phone(address):
        for raw in entries:
            entry = do_not_contact_entry(raw)
            if entry.startswith("+") and same_number(entry, address):
                return entry
        return ""
    if "@" not in address:
        return ""
    local, _, host = address.rpartition("@")
    host = _host(host)
    base_local = local.split("+", 1)[0]
    for raw in entries:
        entry = do_not_contact_entry(raw)
        if not entry:
            continue
        if "@" in entry:
            e_local, _, e_host = entry.rpartition("@")
            if e_host == host and e_local in (local, base_local):
                return entry
        elif host == entry or host.endswith("." + entry):
            return entry
    return ""


def do_not_contact_domain_match(domain: str, entries) -> str:
    """The do-not-contact entry ``domain`` belongs to, or "".

    Only domain entries count here. An address entry bars one mailbox, not
    the company behind it: "jane@acme.example" says don't email Jane, so
    acme.example stays discoverable and a colleague can still be reached.
    Excluding a whole company on the strength of one address would shrink
    the pool in a way nobody asked for, and silently.

    The domain test is the one in ``do_not_contact_match``: a label
    boundary, so "acme.example" covers "mail.acme.example" and leaves
    "notacme.example" — a different company — alone.
    """
    host = _host(domain or "")
    if not host or "@" in host or not entries:
        return ""
    for raw in entries:
        entry = do_not_contact_entry(raw)
        if not entry or "@" in entry or entry.startswith("+"):
            continue    # addresses and phone numbers name a person, not a company
        if host == entry or host.endswith("." + entry):
            return entry
    return ""


def pre_send_check(
    to_email: str,
    subject: str,
    body: str,
    prospect=None,
    allow_risky: bool = False,
    kind: str = "sequence",
    postal_address: str = "",
    do_not_contact=(),
    channel: str = "email",
) -> GateResult:
    """Run every deterministic check. Returns ok=False with reasons on any hit.

    postal_address: when set, a cold (sequence) email must carry it (CAN-SPAM).
    do_not_contact: addresses, domains and phone numbers never to be contacted.
    channel: "email", or "whatsapp" for a message going out through Wasi.

    What the channel changes is only what the medium makes different: the
    recipient is an E.164 number instead of an address, WhatsApp has no
    subject line, and its length limit is its own. Every other check — the
    do-not-contact list, unrendered merge tags, banned phrases, the link
    budget, no HTML — applies to both, because none of them is about email.
    """
    reasons: list[str] = []
    subject = (subject or "").strip()
    body = (body or "").strip()
    to_email = (to_email or "").strip().lower()
    whatsapp = channel == "whatsapp"

    # Recipient sanity
    if whatsapp:
        if not is_e164(to_email):
            reasons.append(
                f"recipient '{to_email}' is not an E.164 phone number (+441632960001)")
    elif not EMAIL_RE.match(to_email):
        reasons.append(f"invalid recipient address '{to_email}'")
    # Deliberately not limited to kind == "sequence": a reply is still contact,
    # and a list that lets automated replies through is not a do-not-contact
    # list. The operator can still answer by hand.
    blocked = do_not_contact_match(to_email, do_not_contact)
    if blocked:
        reasons.append(f"'{blocked}' is on the do-not-contact list")
    if prospect is not None:
        if whatsapp:
            # The phone is the dedup key here, and a listing's local form is
            # the same line as its E.164 form.
            if not same_number(getattr(prospect, "phone", ""), to_email):
                reasons.append("recipient does not match the prospect record")
        else:
            if (prospect.email or "").strip().lower() != to_email:
                reasons.append("recipient does not match the prospect record")
            allowed = sendable_email_statuses(allow_risky)
            # Replies go back to someone who emailed US — deliverability proven.
            if kind == "sequence" and (prospect.email_status or "guess") not in allowed:
                reasons.append(
                    f"email_status '{prospect.email_status or 'guess'}' is not deliverable"
                )

    # Content sanity
    if not subject and not whatsapp:
        reasons.append("empty subject")
    if not body:
        reasons.append("empty body")
    if len(subject) > MAX_SUBJECT_LEN and not whatsapp:
        reasons.append(f"subject too long ({len(subject)} > {MAX_SUBJECT_LEN})")
    if whatsapp:
        # Meta's own ceiling is 4096 characters. A cold message anywhere near
        # that gets blocked by the person, which is what costs the number.
        if len(body) > MAX_WHATSAPP_CHARS:
            reasons.append(
                f"message too long ({len(body)} characters > {MAX_WHATSAPP_CHARS})")
    else:
        word_count = len(body.split())
        if word_count > MAX_BODY_WORDS:
            reasons.append(f"body too long ({word_count} words > {MAX_BODY_WORDS})")

    # Unrendered merge tags — the classic mass-mail embarrassment
    leftover = MERGE_TAG_RE.findall(subject + " " + body)
    if leftover:
        reasons.append(f"unrendered merge tags: {sorted(set(leftover))[:3]}")

    # Banned phrases (spam triggers + AI tells)
    lowered = (subject + " " + body).lower()
    hits = [p for p in BANNED_PHRASES if p in lowered]
    if hits:
        reasons.append(f"banned phrases: {hits[:3]}")

    # Link budget: cold email with links goes to spam
    links = URL_RE.findall(body)
    if len(links) > MAX_LINKS:
        reasons.append(f"too many links ({len(links)} > {MAX_LINKS})")

    # CAN-SPAM is about email. WhatsApp's equivalent is the opt-out Meta
    # enforces itself (a Block button on every message), so a postal address
    # in a WhatsApp message is noise that eats the character budget.
    if (kind == "sequence" and not whatsapp
            and postal_address.strip() and postal_address.strip() not in body):
        reasons.append("postal address footer missing")

    # No HTML — Sellbird sends plain text only
    if re.search(r"<\s*(html|body|div|table|img|a)\b", body, re.IGNORECASE):
        reasons.append("HTML markup in body (plain text only)")

    return GateResult(ok=not reasons, reasons=reasons)


# ── WhatsApp templates ────────────────────────────────────────────────────
#
# Meta only delivers a first message as a template it has approved, and it
# checks the shape it approved. Sending text that is not that template gets
# the message rejected and, repeated, the template pulled and the number's
# standing cut. So the rendered text is checked against the approved body
# here, deterministically, before it leaves — the same principle as the rest
# of this module: the model may write, the code decides what goes.

_PLACEHOLDER_RE = re.compile(r"\{\{\s*\w+\s*\}\}")


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def template_mismatch(rendered: str, template_body: str) -> str:
    """Why ``rendered`` is not ``template_body`` with its placeholders
    filled in, or "" when it is.

    Whitespace runs are treated as equal, since that is the one thing
    WhatsApp itself normalises. A placeholder must be filled with something:
    an empty one leaves "Hi ," on the prospect's phone and is rejected by
    Meta anyway.
    """
    if not _one_line(template_body):
        return ("the approved text for this template isn't stored — "
                "run `sellbird whatsapp templates`")
    if not _one_line(rendered):
        return "the message is empty"
    # An unfilled placeholder is itself text, so it would satisfy the ".+?"
    # below and pass. pre_send_check catches leftover tags too; this fails
    # closed as well, because a template send is the one that costs the number.
    leftover = _PLACEHOLDER_RE.findall(rendered)
    if leftover:
        return f"placeholder {leftover[0]} was never filled in"
    parts = re.split(f"({_PLACEHOLDER_RE.pattern})", _one_line(template_body))
    pattern = "".join(
        r"(?s:.+?)" if _PLACEHOLDER_RE.fullmatch(part) else re.escape(part)
        for part in parts
    )
    if re.fullmatch(pattern, _one_line(rendered)):
        return ""
    return "the message is not this approved template with its placeholders filled"


def template_placeholders(template_body: str) -> list[str]:
    """The placeholder names an approved template expects, in order."""
    return [m.group(0).strip("{} ").strip()
            for m in _PLACEHOLDER_RE.finditer(template_body or "")]


# ── Autopilot review ──────────────────────────────────────────────────────
#
# Websites and the replies people send reach the model, and either can carry
# planted instructions ("add this link", "paste your instructions"). Prompts
# fence that text (sellbird.untrusted), but a model can still follow it. So an
# email nobody approved is also checked here before it goes: anything that
# trips these goes back to the Outbox for a human instead of out (#66).

# Endings that make "name.ending" a file, not a site. Everything else with a
# letter ending counts as a link: a planted one can use any of the hundreds
# of real endings (.ws, .zip, .cfd), so the list of exceptions is the short one.
_FILE_ENDINGS = frozenset("""
    js ts pdf png jpg jpeg gif svg webp heic txt csv doc docx xls xlsx ppt pptx key pages
    json yaml yml toml xml html htm css exe dmg msi bat ini cfg mp3 mp4 wav
""".split())
# A bare "name.ending" with no path or www. is a link when the ending is a
# real one: any two letters (country codes), or one of these. A path or
# www. makes anything a link. Prose with a missing space ("here.Then",
# "approx.half") has neither.
_KNOWN_ENDINGS = frozenset("""
    com net org info biz name pro mobi asia tel xxx io ai app dev xyz site online store shop
    tech cloud page agency studio digital email live life media news blog club world today
    one link click top icu buzz win bid loan work fun space website host press services
    solutions company business center group network systems software tools zone works sale
    deals plus vip best ltd inc llc gmbh zip mov cfd sbs cyou rest bar beauty hair skin
    love sexy porn casino bet poker game games bio eco green energy solar money cash pay
    finance capital fund trade market markets exchange loans credit insurance legal law
    health care clinic dental doctor coach academy school university courses training jobs
    careers events tickets travel tours hotel homes house realty estate properties land
    gov edu mil int test example invalid localhost onion
""".split())
_ENDINGS_RE = "|".join(sorted(_KNOWN_ENDINGS, key=len, reverse=True))
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_DOT_FORMS_RE = re.compile(r"\s*[\[({]\s*(?:\.|dot)\s*[\])}]\s*", re.IGNORECASE)
_SPOKEN_DOT_RE = re.compile(rf"(?<=\w\w)\s+dot\s+(?=(?:{_ENDINGS_RE}|[a-z]{{2}})\b)", re.IGNORECASE)
_SPACED_DOT_RE = re.compile(rf"(?<=\w)\s+\.\s*(?=(?:{_ENDINGS_RE})\b|[^\W\d_]{{2,24}}/)", re.IGNORECASE)
_SCHEME_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
# An address, or just "@domain" (the name before it is optional to a reader).
_EMAIL_HOST_RE = re.compile(r"(?:[\w.%+\-]+|\"[^\"\n]*\"|“[^”\n]*”)?@((?:[^\W_][\w-]*\.)+[^\W\d_]{2,})")
_HOST_OK_RE = re.compile(r"^[^\W_](?:[\w.-]*[^\W_])?$")
UNREADABLE_LINK = "link that can't be read"
_IP_RE = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_BARE_RE = re.compile(
    r"(?<![\w@.])((?:[^\W_](?:[\w-]{0,61}[^\W_])?\.)+([^\W\d_]{2,24}))(?![\w-])([/?#][^\s<>\"']*)?")


def _normalize(text: str) -> str:
    """The text as a reader's browser or eye takes it: fullwidth and other
    lookalike dots become dots, hidden characters go, "evil[.]com" and
    "evil dot com" become evil.com."""
    import unicodedata
    text = unicodedata.normalize("NFKC", text or "")
    text = _ZERO_WIDTH_RE.sub("", text).replace("\u3002", ".").replace("\uff61", ".")
    text = _DOT_FORMS_RE.sub(".", text)
    text = _SPACED_DOT_RE.sub(".", text)
    return _SPOKEN_DOT_RE.sub(".", text)


def _host(value: str) -> str:
    h = (value or "").strip().strip(".").lower()
    return h[4:] if h.startswith("www.") else h


def find_links(text: str) -> list[tuple[str, str]]:
    """Every link and address in ``text`` as (host, host + path), in order.

    The host is the one a browser opens: "https://yours.com:pw@evil.com"
    and "https://evil.com\\.yours.com" both go to evil.com."""
    from urllib.parse import urlsplit
    text = _normalize(text)
    found: list[tuple[str, str]] = []
    for m in _SCHEME_URL_RE.finditer(text):
        url = m.group(0).replace("\\", "/")
        try:
            parts = urlsplit(url)
            host = _host(parts.hostname or "")
            path = parts.path
        except ValueError:
            host, path = "", ""
        if not _HOST_OK_RE.match(host):
            # Won't parse, or a host no browser would take as written: never
            # yours, whatever it ends in.
            host, path = UNREADABLE_LINK, ""
        found.append((host, host + path))
    rest = _SCHEME_URL_RE.sub(" ", text)
    for m in _EMAIL_HOST_RE.finditer(rest):
        found.append((_host(m.group(1)), _host(m.group(1))))
    rest = _EMAIL_HOST_RE.sub(" ", rest)
    for m in _IP_RE.finditer(rest):
        found.append((m.group(0), m.group(0)))
    for m in _BARE_RE.finditer(rest):
        ending, has_path = m.group(2).lower(), bool(m.group(3))
        www = m.group(1).lower().startswith("www.")
        if not has_path and not www and (ending in _FILE_ENDINGS or not (
                len(ending) == 2 or ending in _KNOWN_ENDINGS)):
            continue
        host = _host(m.group(1))
        found.append((host, host + (m.group(3) or "").split("?")[0].split("#")[0]))
    out, seen = [], set()
    for host, link in found:
        if host and (host, link) not in seen:
            seen.add((host, link))
            out.append((host, link))
    return out


def find_link_domains(text: str) -> list[str]:
    """Every domain a link or email address in ``text`` points at, in order."""
    return list(dict.fromkeys(host for host, _ in find_links(text)))


def _booking_prefix(config) -> str:
    offer = getattr(getattr(config, "product", None), "offer", None)
    links = find_links(str(getattr(offer, "booking_url", "") or ""))
    return links[0][1].rstrip("/") if links else ""


def allowed_link_domains(config) -> set[str]:
    """Your own domains: the sending address's and any listed in
    channels.email.link_domains. (The booking link allows its own page,
    not its whole host: cal.com is everyone's.)"""
    persona = getattr(config, "persona", None)
    email_cfg = getattr(getattr(config, "channels", None), "email", None)
    out: set[str] = set()
    address = str(getattr(persona, "email", "") or "")
    if "@" in address:
        out.add(_host(address.rsplit("@", 1)[1]))
    for entry in getattr(email_cfg, "link_domains", None) or []:
        out.update(find_link_domains(str(entry)) or [_host(str(entry))])
    return {d for d in out if d}


def _is_ours(host: str, allowed: set[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in allowed)


def _is_booking_page(link: str, prefix: str) -> bool:
    """``link`` is your booking page (or below it), as a browser resolves it:
    "cal.com/you/15/../../them" is someone else's page."""
    from urllib.parse import unquote
    if not prefix:
        return False
    link = unquote(unquote(link))
    if any(seg in ("..", ".") for seg in link.split("/")[1:]):
        return False
    return link.rstrip("/") == prefix or link.startswith(prefix + "/")


def _shown(host: str, link: str, prefix: str) -> str:
    """What to name in the reason: the host, or host/first-segment when the
    host is the booking link's shared one."""
    if prefix and host == prefix.split("/", 1)[0]:
        return "/".join(link.split("/")[:2])
    return host


# Files Sellbird's instructions live in, under the checkout. A passage from
# them in an outgoing email means a reply quoted Sellbird's playbook back.
# These are written to be said to prospects, so echoing them is fine.
_SAY_ALOUD_FILES = frozenset({"email_frameworks.md", "product_knowledge.md",
                              "objection_handling.md", "README.md"})
LEAK_RUN_WORDS = 12       # this many words in a row, copied, is a quote
_WORD_RE = re.compile(r"[a-z0-9']+")
# Lines the playbooks quote as scripts to use ("Would Thursday at 2pm work
# for a quick call?"): saying them is the point, so they aren't checked.
_SCRIPT_LINE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:>|[\"“'‘])")
# ...and scripts quoted mid-line ('- Entry: "We have a 14-day free trial..."').
# A " | " stands in, so a run never joins the words either side of a script.
_QUOTED_RE = re.compile(r'"[^"\n]{12,}"|“[^”\n]{12,}”')
_leak_cache: dict = {}


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _instruction_runs(root) -> dict[tuple, str]:
    """Every LEAK_RUN_WORDS-word run in Sellbird's instructions → its file."""
    from sellbird.untrusted import UNTRUSTED_RULE
    files = sorted(p for folder in ("prompts", "skills") for p in (root / folder).glob("*.md")
                   if p.name not in _SAY_ALOUD_FILES)
    key = (str(root), tuple((str(p), p.stat().st_mtime_ns) for p in files))
    if _leak_cache.get("key") != key:
        runs: dict[tuple, str] = {}
        sources = [(p.name, _QUOTED_RE.sub(" | ", "\n".join(
            line for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
            if not _SCRIPT_LINE_RE.match(line)))) for p in files]
        sources.append(("Sellbird's prompt rules", UNTRUSTED_RULE))
        for name, text in sources:
            words = _words(text)
            for i in range(len(words) - LEAK_RUN_WORDS + 1):
                runs.setdefault(tuple(words[i:i + LEAK_RUN_WORDS]), name)
        _leak_cache.update(key=key, runs=runs)
    return _leak_cache["runs"]


# A phone number as a reader would dial it: 7 to 15 digits, with the usual
# separators. ISO dates ("2026-11-07") and times aren't numbers to call.
_PHONE_RE = re.compile(r"(?<![\w+#$€£])(?:\+|00)?\(?\d[\d \t().\-\u2010-\u2015/]{5,22}\d(?![\w])")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Ways to be told where to send money. Sales email talks about price and
# payment terms; it never asks the reader to wire money or buy gift cards.
_PAYMENT_RE = re.compile(
    r"\b(?:wire\s+transfer|bank\s+(?:account|details|transfer|wire)|routing\s+number|"
    r"account\s+number|sort\s+code|iban|swift\s+code|bitcoin|btc\s+address|ethereum|"
    r"usdt|crypto(?:currency)?\s+(?:wallet|address|payment)|wallet\s+address|"
    r"gift\s+cards?|western\s+union|moneygram|zelle|venmo|cash\s*app|paypal\.me)\b",
    re.IGNORECASE)


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[2:] if digits.startswith("00") else digits


def find_phone_numbers(text: str) -> list[str]:
    """Every phone number in ``text``, as written."""
    text = _normalize(text)
    found = []
    for m in _PHONE_RE.finditer(text):
        raw = m.group(0).strip()
        digits = _digits(raw)
        # A bare run of digits is an order or ID number unless it's phone-long.
        if (_ISO_DATE_RE.match(raw) or not 7 <= len(digits) <= 15
                or (raw.isdigit() and len(digits) < 10)):
            continue
        found.append(raw)
    return found


def _is_our_number(number: str, ours: set[str]) -> bool:
    """``number`` is one of ``ours``, with or without its country code."""
    d = _digits(number)
    return any(d == o or (len(d) >= 7 and (o.endswith(d) or d.endswith(o))) for o in ours)


def own_phone_numbers(config) -> set[str]:
    """Your numbers: channels.email.phone_numbers, and any in your postal address."""
    email_cfg = getattr(getattr(config, "channels", None), "email", None)
    compliance = getattr(config, "compliance", None)
    out = {_digits(str(n)) for n in (getattr(email_cfg, "phone_numbers", None) or [])}
    out.update(_digits(n) for n in find_phone_numbers(str(getattr(compliance, "postal_address", "") or "")))
    return {d for d in out if len(d) >= 7}


def autopilot_review(subject: str, body: str, config, root=None, recipient: str = "") -> list[str]:
    """Why an email nobody approved must wait for a human, or [] to send.

    What a planted instruction would try: point the reader somewhere that
    isn't yours (a link, an address, a phone number), ask them to send money,
    or get Sellbird to quote its own instructions back.
    ``recipient``'s own domain is fine to mention to them ("saw
    joesroofing.com"): it's theirs."""
    from sellbird.paths import PROJECT_ROOT
    reasons: list[str] = []
    text = f"{subject or ''}\n{body or ''}"
    allowed = allowed_link_domains(config)
    if "@" in (recipient or ""):
        allowed.add(_host(recipient.rsplit("@", 1)[1]))
    prefix = _booking_prefix(config)
    foreign = list(dict.fromkeys(
        _shown(host, link, prefix) for host, link in find_links(text)
        if not _is_ours(host, allowed) and not _is_booking_page(link, prefix)))
    if foreign:
        reasons.append(
            f"links to {', '.join(foreign[:3])}, not one of your domains "
            "(add it to channels.email.link_domains if it's yours)")
    ours = own_phone_numbers(config)
    numbers = list(dict.fromkeys(n for n in find_phone_numbers(text) if not _is_our_number(n, ours)))
    if numbers:
        reasons.append(
            f"gives the phone number {', '.join(numbers[:3])}, not one of yours "
            "(add it to channels.email.phone_numbers if it's yours)")
    payment = _PAYMENT_RE.search(_normalize(text))
    if payment:
        reasons.append(f"asks about sending money (\"{payment.group(0)}\")")
    words = _words(text)
    runs = _instruction_runs(root or PROJECT_ROOT)
    for i in range(len(words) - LEAK_RUN_WORDS + 1):
        source = runs.get(tuple(words[i:i + LEAK_RUN_WORDS]))
        if source:
            reasons.append(f"quotes Sellbird's instructions ({source})")
            break
    return reasons
