"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

from sellbird.brain import Brain
from sellbird.config import (
    SellbirdConfig, EnvConfig, cold_daily_cap, compliance_footer_lines, compliance_notice,
    compliance_of, live_do_not_contact, operator_timezone, send_window_of,
)
from sellbird import extensions
from sellbird.gate import (
    EMAIL_RE, autopilot_review, do_not_contact_match, pre_send_check,
    render_template, sendable_email_statuses, template_mismatch,
    template_param_problem, template_placeholders,
)
from sellbird.send_hooks import NO_HEADERS_HOLD, SendCheck, SendHookError, extra_headers, first_verdict
from sellbird.send_window import in_window, next_send_time, recipient_country, recipient_zone
from sellbird.integrations.instantly import InstantlyClient
from sellbird.integrations.mail_provider import (
    NATIVE_PROVIDERS, get_mail_provider, get_whatsapp_provider,
)
from sellbird.integrations.precleared import (
    UNKNOWN_ATTEMPTS, UNKNOWN_RETRY_AFTER, PreclearedClient, clear_hold, record_hold,
)
from sellbird.integrations.signature import signature_problem
from sellbird.integrations.wasi import DEAD_NUMBER_META_CODES, WINDOW_CLOSED
from sellbird.email_style import style_problems
from sellbird.mailboxes import Inboxes, load_inboxes
from sellbird.phone import normalize as normalize_phone
from sellbird.sending_mode import SIGNATURE_HOLD_PREFIX, sync_sending_mode
from sellbird.state import (
    AUTOPILOT_OFF_REASON, COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW,
    KILL_SWITCH_KEY, MANUAL_SEND_KEY, MAYBE_SENT_PREFIX, MOVED_BACK_PREFIXES,
    RECHECK_PREFIX, UNKNOWN_HOLD_PREFIX, StateManager,
)

# A follow-up waits at least this long after the email it follows went out,
# whatever its schedule says.
MIN_FOLLOW_UP_GAP = timedelta(days=1)

logger = logging.getLogger("sellbird.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
# How many due emails quiet hours look through for one that can go now.
QUIET_HOURS_SCAN = 200
SEND_JITTER_SECONDS = (4, 15)
# Optional Precleared check: an email whose address Precleared couldn't
# check is moved back this far, behind other email, rather than asked about
# again every cycle. Failures for this many different domains with no answer
# in between (across cycles) mean Precleared itself is in trouble, so checks
# pause. And no new check starts after this many seconds of checking in one
# cycle.
ADDRESS_RETRY_AFTER = timedelta(hours=6)
DOMAINS_FAILING_TO_PAUSE = 2
CHECK_SECONDS_PER_CYCLE = 90

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}


# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}



def _utcnow() -> datetime:
    """Naive UTC now, as the outbox stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_now() -> datetime:
    """Timezone-aware local now, for "today" counted from local midnight.
    Tests pin it."""
    return datetime.now().astimezone()


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: SellbirdConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        # The WhatsApp channel, or None when it is off or unconfigured.
        self.whatsapp = get_whatsapp_provider(config, env)
        # Every inbox this cycle may send from (sellbird.mailboxes), read once
        # per run; None outside a run. Tests pass a SecretStore for extra inboxes.
        self.inboxes: Inboxes | None = None
        self.secrets = None
        self._inbox_providers: dict = {}   # extra inboxes' providers, kept across cycles
        # Optional per-recipient compliance check; None unless enabled.
        self.precleared = PreclearedClient.from_config(config, env)
        # Domains whose check failed since Precleared last answered. Kept
        # across cycles: one new address a cycle failing is still a pattern.
        self._failing_domains: set[str] = set()
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        # Set by the heartbeat in quiet hours (send windows in recipients'
        # zones): only cold email goes, inside its window; replies wait for
        # the operator's own hours, as they always have.
        self.cold_only = False
        # The last drain couldn't ask Precleared: quiet hours wake only for
        # cold email it has already cleared until a drain outside them has run.
        self.compliance_held = False

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def _send_zone(self, prospect, country: str = "", given: str = "",
                         names: tuple[str, str] = ("", "")) -> str:
        """The time zone this recipient's send window is in: their own when
        it can be told (with recipient_timezone on), otherwise the zone in
        usage.quiet_hours, as before. ``country`` is the recipient's country
        from Precleared, ``given`` the one Sellbird gave the check (see
        _recipient_country) and ``names`` the company and person names it
        gave (see _recipient_names), which a cached verdict is keyed on."""
        window = send_window_of(self.config)
        if window is not None and window.recipient_timezone:
            if not country and self.precleared is not None and prospect is not None:
                # A country Precleared already found counts on every check,
                # not only on the one that asked it.
                company, name = names
                country = await self.precleared.known_country(
                    prospect.email, self.state, given, company=company, name=name)
            zone = await recipient_zone(self.state, prospect, self.config, country)
            if zone:
                return zone
        return operator_timezone(self.config)

    async def _zone_or_default(self, prospect, country: str = "", given: str = "",
                               names: tuple[str, str] = ("", "")) -> str:
        """_send_zone, but a failure to work the zone out falls back to
        usage.quiet_hours.timezone for this one email instead of stopping
        the whole drain (replies included)."""
        try:
            return await self._send_zone(prospect, country, given, names)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the time zone for "
                f"{getattr(prospect, 'email', '?')}, using quiet_hours.timezone: {e}")
            return operator_timezone(self.config)

    async def _recipient_country(self, prospect) -> str:
        """The recipient's country when Sellbird is sure of it, for the
        compliance check (see send_window.recipient_country). A failure to
        work it out means none for this one email, which Precleared then
        works out itself, instead of stopping the drain."""
        try:
            return await recipient_country(self.state, prospect)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the country for "
                f"{getattr(prospect, 'email', '?')}, leaving it to Precleared: {e}")
            return ""

    async def _recipient_names(self, prospect) -> tuple[str, str]:
        """The company's name and the person's full name, for the compliance
        check: the company record's name, else the prospect's own company
        field, and the prospect's first and last name. A company inbox has no
        person, so no name. A failure to load the company record falls back
        to the prospect's own field for this one email instead of stopping
        the drain."""
        if prospect is None:
            return "", ""
        company = ""
        if getattr(prospect, "company_id", ""):
            try:
                record = await self.state.get_company(prospect.company_id)
                company = (getattr(record, "name", "") or "") if record else ""
            except Exception as e:
                logger.warning(
                    f"Sender: couldn't load the company for "
                    f"{getattr(prospect, 'email', '?')}, using the prospect's own: {e}")
        company = company.strip() or (getattr(prospect, "company", "") or "").strip()
        name = ""
        if not getattr(prospect, "is_company_inbox", False):
            first = (getattr(prospect, "first_name", "") or "").strip()
            last = (getattr(prospect, "last_name", "") or "").strip()
            name = f"{first} {last}".strip()
        return company, name

    async def _has_cached_verdict(self, item: dict) -> bool:
        """Use the send loop's recipient facts when counting held email."""
        prospect = await self.state.get_prospect(item["prospect_id"])
        country = await self._recipient_country(prospect)
        company, name = await self._recipient_names(prospect)
        return await self.precleared.cached(
            item["to_email"], self.state, country, company=company, name=name) is not None

    async def cold_due_in_window(self) -> bool:
        """Quiet hours: whether a cold email is due, inside its recipient's
        send window and free to go now (sending not paused, under the daily
        and warmup caps, and after a compliance hold only to someone
        Precleared has already cleared). Only then is it worth waking to read
        replies and send."""
        window = send_window_of(self.config)
        if window is None or not self.is_native:
            return False
        if await self.state.get_setting(KILL_SWITCH_KEY):
            return False
        email = self.config.channels.email
        if email.max_daily_sends - await self.state.count_outbox_sent_today(
                channel="email") <= 0:
            return False
        inboxes = await self._load_inboxes()
        if inboxes.several:
            # Any inbox with cold room left today is worth waking for.
            if not any(i.cold_left > 0 for i in inboxes.sendable()):
                return False
        else:
            if not self.provider.is_configured():
                return False
            midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            if cold_daily_cap(email) - await self.state.count_outbox_sent_today(
                    kind="sequence", channel="email",
                    since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()) <= 0:
                return False
        now = _utcnow()
        due_before = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=due_before,
            limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, kind="sequence",
        )
        if self.compliance_held and self.precleared is not None:
            # Match the drain's separate follow-up scan: waiting first emails
            # must not hide a cleared follow-up and prevent the loop waking.
            seen = {item["id"] for item in due}
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=due_before,
                limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, follow_ups_only=True,
            )
            due += [item for item in follow_ups if item["id"] not in seen]
        for item in due:
            if item["kind"] != "sequence":
                continue
            prospect = await self.state.get_prospect(item["prospect_id"])
            country = await self._recipient_country(prospect) if self.precleared is not None else ""
            names = await self._recipient_names(prospect) if self.precleared is not None else ("", "")
            if self.compliance_held and self.precleared is not None:
                # New addresses wait for a drain outside quiet hours; a
                # stored verdict still lets email go, as in the drain.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=names[0], name=names[1])
                if clearance is None or not clearance.allowed:
                    continue
            if prospect is not None and in_window(
                    now, await self._zone_or_default(prospect, given=country, names=names), window):
                return True
        return False

    async def _outside_window(self, item: dict, zone: str, window) -> bool:
        """Move a cold email that isn't in its send window to the window's
        next opening. True when it was moved and must wait."""
        now = _utcnow()
        if in_window(now, zone, window):
            return False
        later = next_send_time(now, zone, window)
        await self.state.update_outbox_item(item["id"], send_at=later.isoformat())
        return True

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if self.whatsapp is not None:
            # Its own channel: it runs even for an install that only uses
            # WhatsApp, and a failure there never stops email going out.
            try:
                await self._drain_whatsapp()
            except Exception as e:
                logger.error(f"Sender: the WhatsApp drain failed: {e}")
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    def _reviewed_leads(self, prospects: list) -> list:
        """The prospects whose merge fields pass the autopilot review."""
        kept = []
        for p in prospects:
            fields = " \n".join(str(v or "") for v in (
                p.first_name, p.last_name, p.company, p.title, p.personalization_notes))
            held = autopilot_review("", fields, self.config, recipient=p.email)
            if listed := do_not_contact_match(p.email, live_do_not_contact(self.config)):
                held = [f"'{listed}' is on the do-not-contact list", *held]
            if held:
                logger.warning(f"Sender: left {p.email} out of the Instantly upload: {held}")
            else:
                kept.append(p)
        return kept

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        # Instantly owns campaign scheduling and has no per-message outbox
        # approval here. Fail closed rather than silently bypass the setting,
        # including retries of an already-created remote campaign.
        await sync_sending_mode(self.config, self.state)
        if getattr(self.config.channels.email, "require_approval", True):
            logger.warning(
                "Sender: Instantly campaign held because require_approval is true. "
                "Use a native mail provider for outbox review, or explicitly set "
                "channels.email.require_approval: false for Instantly autopilot."
            )
            return
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return
        # Instantly only deploys on autopilot, so nobody has read these
        # templates: what a planted instruction would add holds the campaign.
        held = [f"step {s.step} {r}" for s in campaign.sequence
                for r in autopilot_review(s.subject, s.body, self.config)]
        if held:
            logger.warning(f"Sender: Instantly campaign '{campaign.name}' held for review: {held}")
            key = "instantly_review_hold:" + campaign.id
            if not await self.state.get_setting(key):
                await self.state.set_setting(key, "1")
                await self.state.log_action(
                    action_type="escalation", agent="sender",
                    details={"campaign_name": campaign.name,
                             "reason": "campaign held: " + "; ".join(held)[:200]},
                )
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": self._with_footer(step.body.strip()),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        await self.state.remember_footer_lines(compliance_footer_lines(self.config))
        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = sendable_email_statuses(allow_risky)
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                continue  # the user emailed them by hand
            if await self.state.is_suppressed(email):  # never emailed, on Instantly too
                logger.debug(f"Sender: Skipping {email} (suppressed).")
                continue
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        # Instantly fills these into the templates on its side, unseen:
        # scraped text carrying a planted link keeps that lead out.
        prospects = self._reviewed_leads(prospects)
        if not prospects:
            return
        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today (the local day).

        prospects.updated_at is stored as naive UTC, so "today" is measured
        from local midnight converted to UTC. Matching the stored text
        against the local date would shift the day by the UTC offset.
        """
        from sellbird import storage
        midnight = _local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        async with storage.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects "
                "WHERE status = 'contacted' AND datetime(updated_at) >= datetime(?)",
                (since,),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        # Before any early return: a send cut off mid-way must show up as
        # failed even while sending is paused or no mailbox is connected.
        stale = await self.state.fail_stale_sending_outbox()
        if stale:
            logger.warning(
                f"Sender: {stale} email(s) were interrupted mid-send and won't be "
                "retried. Check your Sent folder; they're under Didn't send."
            )
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `sellbird sending resume` once the cause is fixed."
            )
            return

        # Read once, before staging, so a new thread only goes to an inbox
        # that can send, and each pick sees the threads assigned before it.
        self.inboxes = await self._load_inboxes()
        try:
            for campaign in await self.state.get_campaigns_by_status("draft"):
                try:
                    await self._stage_campaign_native(campaign)
                except Exception as e:
                    logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

            await self._drain_due()
        finally:
            self.inboxes = None

    async def _load_inboxes(self) -> Inboxes:
        return await load_inboxes(self.state, self.config, self.env, self.provider,
                                  secrets=self.secrets, cache=self._inbox_providers)

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            # A company inbox has no name; "there" keeps "Hi {{first_name}}," readable.
            "first_name": prospect.first_name or ("there" if prospect.is_company_inbox else ""),
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        # "{{company}}'s" for "Parkes Builders" should read "Builders'".
        company = prospect.company or ""
        if company.endswith(("s", "S")):
            for tag in ("{{company}}", "{{ company }}"):
                text = text.replace(tag + "'s", company + "'")
                text = text.replace(tag + "’s", company + "’")
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = sendable_email_statuses(allow_risky)

        staged = 0
        skipped = 0
        answered = 0
        now = _utcnow()
        seen_emails: set[str] = set()
        window = send_window_of(self.config)

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if await self.state.is_suppressed(email):  # never staged
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            if await self.state.colleague_who_replied(prospect.id):
                answered += 1     # their company already answered
                continue
            seen_emails.add(email)

            # With a send window, each email is planned for the recipient's
            # office hours; follow-ups keep their delay, then move into it.
            zone = await self._zone_or_default(prospect) if window is not None else ""
            cumulative_days = 0
            new_rows = 0
            for step in campaign.sequence:
                if step.step > 1 and not getattr(self.config.channels.email, "follow_ups", True):
                    continue    # first email only; a reply is still answered
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                if window is not None:
                    send_at = next_send_time(send_at, zone, window)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=self._render(step.body, prospect),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                    approved_by="" if require_approval else "autopilot",
                )
                if item_id:
                    staged += 1
                    new_rows += 1
            if new_rows and self.inboxes is not None and self.inboxes.several:
                # The thread's inbox: every one of its emails leaves from it.
                await self.inboxes.assign(campaign.id, prospect.id, staged_now=True)
            # Conditional: never overwrite a status set while staging
            # (marked sent by hand, a reply).
            await self.state.queue_prospect_if_unsent(prospect.id)

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if answered:
            logger.info(
                f"Sender: left out {answered} prospect(s) from '{campaign.name}': "
                f"{COLLEAGUE_REPLIED} at their company."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    def _with_footer(self, body: str) -> str:
        """A cold email as it leaves: the postal address and opt-out line
        underneath (CAN-SPAM). Added at send time, so a changed address
        applies to mail already queued; not added again when the body already
        ends with it."""
        footer = "\n".join(compliance_footer_lines(self.config))
        if not footer or body.rstrip().endswith(footer):
            return body
        return body.rstrip() + "\n\n" + footer

    def _outgoing_body(self, item: dict) -> str:
        """The exact body that goes out: cold email gets the footer, replies
        don't, and WhatsApp never does — Meta puts a Block button on every
        message, so a postal address there only eats the character budget."""
        if item["kind"] == "sequence" and (item.get("channel") or "email") == "email":
            return self._with_footer(item["body"])
        return item["body"]

    async def _follow_up_order(self, item: dict, prospect=None) -> str | tuple[str, str] | None:
        """For a follow-up: "wait" until the email before it went out and its
        gap has passed, (status, error) when that email never will, else None.

        Without this, a step held for any reason (approval, a pause, a
        compliance hold, the warmup ramp) lets its follow-ups' scheduled
        times arrive first: a late "Approve all" sent the whole sequence in
        one go, or step 2 before step 1.
        """
        if item["kind"] != "sequence" or int(item["step"]) <= 1:
            return None
        prev = await self.state.get_previous_step(item)
        if prev is None:
            return None
        if prev["status"] in ("pending_review", "approved", "sending"):
            return "wait"
        if prev["status"] != "sent":
            return "cancelled", f"step {prev['step']} wasn't sent ({prev['status']})"

        def _at(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None
        sent_at = _at(prev["sent_at"])
        if sent_at is None:
            return None
        delay = await self.state.get_step_delay_days(item.get("campaign_id"), item["step"])
        if delay is None:  # campaign unknown: fall back to the planned spacing
            planned, prev_planned = _at(item["send_at"]), _at(prev["send_at"])
            gap = (planned - prev_planned) if planned and prev_planned else MIN_FOLLOW_UP_GAP
        else:
            gap = timedelta(days=delay)
        ready_at = sent_at + max(MIN_FOLLOW_UP_GAP, gap)
        # Each channel has its own window: rescheduling a WhatsApp follow-up
        # into email's office hours would be the wrong promise to keep.
        window = (self.config.channels.whatsapp.send_window
                  if (item.get("channel") or "email") == "whatsapp"
                  else send_window_of(self.config))
        if window is not None and prospect is not None:
            ready_at = next_send_time(ready_at, await self._zone_or_default(prospect), window)
        if _utcnow() >= ready_at:
            return None
        # Reschedule it for then, so it leaves the due queue until it's ready.
        await self.state.update_outbox_item(item["id"], send_at=ready_at.isoformat())
        return "wait"

    async def _retry_later(self, item: dict, reason: str, after: timedelta = ADDRESS_RETRY_AFTER,
                           prefix: str = RECHECK_PREFIX) -> datetime:
        """Move a cold email back by ``after`` (ADDRESS_RETRY_AFTER unless
        said otherwise), saying why; return the new time. Sending it clears
        the note."""
        retry_at = _utcnow() + after
        await self.state.update_outbox_item(
            item["id"], send_at=retry_at.isoformat(),
            error=f"{prefix}{retry_at:%Y-%m-%d %H:%M} UTC: {reason}"[:300])
        return retry_at

    async def _blocked(self, item: dict, prospect, allow_risky: bool) -> tuple[str, str] | None:
        """Why this email must not go out, as (status, error), or None."""
        await sync_sending_mode(self.config, self.state)   # "Turn off autopilot" counts at once
        if prospect is None:
            return "failed", "prospect missing"
        # A suppressed address or domain is never emailed, whatever the kind.
        suppressed = await self.state.is_suppressed(item["to_email"])
        if suppressed:
            return "cancelled", f"suppressed ({suppressed})"
        if item["kind"] == "sequence":
            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if prospect.status in STOP_STATUSES:
                return "cancelled", f"prospect status '{prospect.status}'"
            # The user already emailed them by hand: whatever Sellbird staged
            # (before, during or after that) would be a second email.
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                return "cancelled", HAND_SENT_REASON
            # A first email to someone already contacted is always a duplicate.
            if item["step"] == 1 and prospect.status == "contacted":
                return "cancelled", "already contacted"
            # Someone else at their company answered: the rest of the
            # company gets no more cold email.
            colleague = await self.state.colleague_who_replied(prospect.id)
            if colleague:
                return "cancelled", f"{COLLEAGUE_REPLIED} ({colleague})"
        channel = item.get("channel") or "email"
        # Sellbird's writing rules (no dashes, no exclamation marks, no AI
        # phrases): a draft that breaks them waits for an edit instead of
        # going out. Length and shape were checked when it was written, so an
        # edit you made on purpose isn't second-guessed here.
        #
        # A WhatsApp template is exempt: its words are the user's own, already
        # approved by Meta, and Sellbird did not write them. Holding every cold
        # WhatsApp message because an approved template has an exclamation mark
        # would hold them all, for ever, with no edit that could help.
        templated = bool(channel == "whatsapp" and item.get("subject"))
        style = [] if templated else style_problems(
            item["subject"] if channel == "email" else "", item["body"],
            kind="reply" if item["kind"] == "reply" else "cold",
            names=_names(prospect), shape=False)
        if style:
            logger.warning(f"Sender: holding message to {item['to_email']}: {style}")
            return "pending_review", (HELD_FOR_REVIEW + "the message " + "; ".join(style))[:300]
        gate = pre_send_check(
            item["to_email"], item["subject"] if channel == "email" else "",
            self._outgoing_body(item),
            prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            postal_address=compliance_of(self.config).postal_address,
            do_not_contact=live_do_not_contact(self.config),
            channel=channel,
        )
        if not gate:
            logger.warning(
                f"Sender: gate blocked the message to {item['to_email']}: "
                f"{gate.reasons}"
            )
            # A reply that is only too long is an edit away from sendable, so
            # it waits for a human instead of becoming a dead row — otherwise
            # the person's question is answered by nobody. Everything else the
            # gate stops (a bad recipient, a suppressed address, markup) is
            # terminal, as before.
            if (item["kind"] == "reply"
                    and all("too long" in reason for reason in gate.reasons)):
                return "pending_review", (HELD_FOR_REVIEW + "; ".join(gate.reasons))[:300]
            return "failed", "gate: " + "; ".join(gate.reasons)[:300]
        # Each channel has its own approval setting, so a WhatsApp row must be
        # judged by channels.whatsapp.require_approval. Reading email's for
        # every channel sent WhatsApp autopilot rows back for review for ever,
        # since email defaults to requiring approval.
        channel_settings = (getattr(self.config.channels, "whatsapp", None)
                            if channel == "whatsapp" else self.config.channels.email)
        if (item.get("approved_by") == "autopilot"
                and getattr(channel_settings, "require_approval", True)):
            # Autopilot approved it, and autopilot has since been turned off.
            return "pending_review", AUTOPILOT_OFF_REASON
        if item.get("approved_by") != "you":
            # Nobody approved this one (autopilot): what a planted
            # instruction would add sends it back to you instead.
            held = autopilot_review(item["subject"], self._outgoing_body(item), self.config,
                                    recipient=item["to_email"])
            if held:
                logger.warning(f"Sender: holding email to {item['to_email']} for review: {held}")
                return "pending_review", (HELD_FOR_REVIEW + "; ".join(held))[:300]
        # Extensions' send rules (sellbird.send_hooks): after every built-in
        # check, so they can only hold or cancel, never let through.
        rules = extensions.registry().send_rules
        if rules:
            verdict = await first_verdict(rules, SendCheck(
                item=item, prospect=prospect, config=self.config, state=self.state))
            if verdict:
                logger.info(f"Sender: {verdict[1]} ({item['to_email']})")
                return verdict
        return None

    async def _drain_due(self):
        """Send due, approved outbox items through the provider."""
        if self.precleared is None:
            # Check switched off: cold email no longer waits on it, so a
            # hold recorded while it was on must not stay on Today.
            await clear_hold(self.state)
        loaded = self.inboxes if self.inboxes is not None else await self._load_inboxes()
        # Several inboxes: each email is routed to its thread's inbox. One
        # inbox (the main mailbox only): exactly as before.
        inboxes = loaded if loaded.several else None
        if inboxes is None and not self.provider.is_configured():
            logger.warning(
                f"Sender: mail provider '{self.provider.name}' is not configured "
                "yet — outbox is holding. See CLAUDE.md → provider setup."
            )
            return
        if inboxes is not None and not inboxes.sendable():
            logger.warning("Sender: no inbox can send right now (each is paused or not "
                           "connected) — outbox is holding. See `sellbird inboxes list`.")
            return
        # Every email carries the configured logo, or none goes out: a
        # broken logo holds approved email rather than failing it.
        problem = signature_problem(self.config)
        if problem:
            logger.warning(f"Sender: email signature can't be used ({problem}) — outbox is holding.")
            await self.state.note_sends_held(f"{SIGNATURE_HOLD_PREFIX}{problem}")
            return

        max_daily = self.config.channels.email.max_daily_sends
        # Email only: channels.email.max_daily_sends is email's cap, and
        # WhatsApp has its own. Counting both here meant a WhatsApp message
        # silently took an email out of the day's allowance.
        sent_today = await self.state.count_outbox_sent_today(channel="email")
        # Each inbox sends up to MAX_SENDS_PER_CYCLE a cycle; max_daily_sends
        # stays the ceiling on everything sent in a day.
        per_cycle = MAX_SENDS_PER_CYCLE * (len(inboxes.sendable()) if inboxes else 1)
        budget = min(per_cycle, max_daily - sent_today)
        if budget <= 0:
            logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}).")
            return
        # Cold email has its own cap under the warmup ramp; replies don't.
        # Counted from local midnight, the day the ramp counts in.
        cold_cap = cold_daily_cap(self.config.channels.email)
        midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        cold_left = cold_cap - await self.state.count_outbox_sent_today(
            kind="sequence", channel="email",
            since=midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat())

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        # In quiet hours only cold email can go, so only cold email is
        # fetched: replies due ahead of it can't fill the page.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            skip_unready_follow_ups=True, kind="sequence" if self.cold_only else None,
        )
        # Cold emails held (by an unavailable compliance check, the cold
        # cap, a follow-up's spacing) stay approved and could fill the
        # page above, so replies (and any other kind) are fetched on
        # their own too and never wait behind them. Bounded: one extra
        # query of the same size.
        seen = {item["id"] for item in due}
        unchecked = [] if self.cold_only else await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3,
            exclude_kind="sequence",
        )
        extra = [item for item in unchecked if item["id"] not in seen]
        if self.precleared is not None:
            # First emails waiting on Precleared keep their place and could
            # fill the page too, hiding follow-ups to people already cleared,
            # which a stored verdict lets go out. Fetched on their own as well.
            seen.update(item["id"] for item in extra)
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=now, limit=budget * 3,
                skip_unready_follow_ups=True, follow_ups_only=True,
            )
            extra += [item for item in follow_ups if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        if not due:
            return

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        window = send_window_of(self.config)
        sent = 0
        sent_cold = 0
        outside_window = 0
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        held_cold = 0
        # For the Today page and `sellbird status`: what held cold email this
        # cycle, how many waited behind it, and whether a fresh call was
        # answered (which clears an earlier hold).
        hold = None
        waiting_on_check = 0
        answered = False
        # Cold emails held this cycle on an "unknown" answer (a fact Precleared
        # couldn't establish), asked about again later.
        unknown_held = 0
        # Addresses Precleared couldn't check this cycle: their other emails
        # aren't asked about again. (self._failing_domains: failures across
        # several domains pause checks.)
        failed_addresses: set[str] = set()
        check_seconds = 0.0
        # Several inboxes: why emails waited on their inbox, by reason.
        inbox_waits: dict[str, int] = {}
        for item in due:
            if sent >= budget:
                break
            if self.cold_only and item["kind"] != "sequence":
                continue  # quiet hours: replies wait for the operator's hours
            provider, inbox = self.provider, None
            if inboxes is not None:
                route = await inboxes.route(item)
                if route.action == "cancel":
                    await self.state.update_outbox_item(
                        item["id"], status="cancelled", error=route.reason)
                    continue
                if route.action == "hold":
                    inbox_waits[route.reason] = inbox_waits.get(route.reason, 0) + 1
                    continue
                inbox = inboxes.get(route.mailbox_id)
                if inbox.sent_this_cycle >= MAX_SENDS_PER_CYCLE:
                    continue
                if item["kind"] == "sequence" and inbox.cold_left <= 0:
                    held_cold += 1
                    continue
                provider = inbox.provider
            elif item["kind"] == "sequence" and sent_cold >= cold_left:
                held_cold += 1
                continue
            needs_check = self.precleared is not None and item["kind"] == "sequence"

            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            order = await self._follow_up_order(item, prospect)
            if order == "wait":
                continue
            if order:
                await self.state.update_outbox_item(item["id"], status=order[0], error=order[1])
                continue
            # The recipient's country when Sellbird is sure of it, and the
            # company's and person's names: they go with the compliance
            # check, and a cached verdict is keyed on them.
            country = await self._recipient_country(prospect) if needs_check else ""
            names = await self._recipient_names(prospect) if needs_check else ("", "")
            # Cold email waits for the recipient's send window, however
            # late it was approved; replies don't.
            if (window is not None and item["kind"] == "sequence"
                    and await self._outside_window(
                        item, await self._zone_or_default(prospect, given=country, names=names),
                        window)):
                outside_window += 1
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                company, name = names
                # A stored verdict still counts once checks are paused, so
                # people already cleared keep getting their follow-ups; only
                # new addresses wait for the next cycle.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=company, name=name)
                if clearance is None and (compliance_paused
                                          or check_seconds > CHECK_SECONDS_PER_CYCLE):
                    waiting_on_check += 1
                    continue
                address = (item["to_email"] or "").strip().lower()
                if clearance is None and address in failed_addresses:
                    await self._retry_later(item, "the check failed for this address earlier this cycle")
                    continue
                if clearance is None:
                    started = time.monotonic()
                    clearance = await self.precleared.check(
                        item["to_email"], self.state, country, company=company, name=name)
                    check_seconds += time.monotonic() - started
                if clearance.held and not clearance.pause_checks:
                    # Possibly about this address alone: move its email back,
                    # behind other email, and carry on with the rest. Only a
                    # first failure counts towards pausing: an address failing
                    # again says nothing new about Precleared.
                    again = (item.get("error") or "").startswith(RECHECK_PREFIX)
                    failed_addresses.add(address)
                    if not again:
                        self._failing_domains.add(address.rpartition("@")[2])
                    waiting_on_check += 1
                    retry_at = await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: Precleared couldn't check {item['to_email']}, trying "
                        f"again after {retry_at:%Y-%m-%d %H:%M} UTC: {clearance.reason}"
                    )
                    if again or len(self._failing_domains) < DOMAINS_FAILING_TO_PAUSE:
                        continue
                if clearance.held:
                    # About Precleared, not just this address: the others
                    # would likely fail too, so no more fresh checks this cycle.
                    compliance_paused = True
                    hold = clearance
                    if clearance.pause_checks:  # else counted just above
                        waiting_on_check += 1
                        if clearance.retry_address:
                            # It may still be this address: don't let it be
                            # first in line, and asked first, every cycle.
                            await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.cached:
                    answered = True
                    self._failing_domains.clear()
                if clearance.verdict == "unknown" and not clearance.unknown_final:
                    # Not a verdict but a fact Precleared couldn't establish
                    # (usually the recipient's country). Hold the email and
                    # ask again later, in case the fact turns up: a slow
                    # register answers, or the user sets prospect_country or
                    # adds the company's location. The note says what would
                    # settle it. The first retry is soon, for a passing
                    # problem; later ones leave time to fix the config.
                    after = ADDRESS_RETRY_AFTER if clearance.unknown_attempts <= 1 else UNKNOWN_RETRY_AFTER
                    retry_at = await self._retry_later(
                        item, clearance.reason, after=after, prefix=UNKNOWN_HOLD_PREFIX)
                    unknown_held += 1
                    logger.warning(
                        f"Sender: Precleared couldn't settle {item['to_email']} "
                        f"(unknown, answer {clearance.unknown_attempts} of "
                        f"{UNKNOWN_ATTEMPTS}); trying again after "
                        f"{retry_at:%Y-%m-%d %H:%M} UTC: {clearance.reason}"
                    )
                    continue
                if not clearance.allowed:
                    verdict = clearance.verdict
                    if verdict == "unknown":
                        # UNKNOWN_ATTEMPTS answers of unknown on the same facts.
                        verdict = f"unknown after {clearance.unknown_attempts} checks"
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared held back email to {item['to_email']}: "
                        f"{verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id
                # Precleared found where the recipient is, and nothing else
                # had: their own window applies from now on.
                if (window is not None and window.recipient_timezone and clearance.country
                        and not prospect.timezone):
                    zone = await self._zone_or_default(prospect, clearance.country)
                    if zone and await self._outside_window(item, zone, window):
                        outside_window += 1
                        continue

            # Claim it. A reply, bounce, hand send or rejection may have
            # cancelled it since the queue was read; then it stays cancelled.
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            # Send the email as it is now, not as the queue read it: it may
            # have been edited and approved again since.
            item = await self.state.get_outbox_item(item["id"]) or item
            # Whatever landed between the checks above and the claim (a reply
            # sets the status before it cancels) is caught here, with fresh
            # data. From now on nothing else can cancel this email.
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue

            body = self._outgoing_body(item)
            try:
                headers = extra_headers(extensions.registry().send_headers, item)
            except SendHookError as e:
                logger.error(f"Sender: holding email to {item['to_email']}: {e}")
                await self.state.update_outbox_item(item["id"], status="approved",
                                                    error=f"held: {e}"[:300])
                continue
            if headers and not getattr(provider, "carries_headers", False):
                # Never send it without them: it waits for an inbox that can.
                await self.state.update_outbox_item(item["id"], status="approved",
                                                    error=f"held: {NO_HEADERS_HOLD}")
                continue
            extra = {"headers": headers} if headers else {}
            try:
                result = await provider.send_email(
                    item["to_email"], item["subject"], body,
                    thread_ref=item.get("thread_ref", ""),
                    in_reply_to=item.get("in_reply_to", ""),
                    **extra,
                )
            except Exception as e:
                # It may have gone out before the error: never retry it.
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"{MAYBE_SENT_PREFIX}send error: {e}"[:300],
                )
                logger.warning(f"Sender: send to {item['to_email']} raised: {e}")
                continue
            if not result.ok and result.error.startswith("signature:"):
                # The logo broke after this drain checked it, and the provider
                # refused before sending: this email waits, like the others.
                await self.state.update_outbox_item(item["id"], status="approved")
                await self.state.note_sends_held(SIGNATURE_HOLD_PREFIX + result.error[10:].strip())
                logger.warning(f"Sender: {result.error} — outbox is holding.")
                break
            if not result.ok:
                # The provider says it may have accepted it: never retry, and
                # say so, as for a send that raised.
                prefix = MAYBE_SENT_PREFIX if getattr(result, "uncertain", False) else ""
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=f"{prefix}{result.error}"[:300]
                )
                continue

            sent += 1
            if item["kind"] == "sequence":
                sent_cold += 1
            if inbox is not None:
                inbox.sent_this_cycle += 1
                if item["kind"] == "sequence":
                    inbox.sent_today += 1
                # Which inbox and provider it actually left from.
                note_inbox = {"mailbox_id": inbox.id, "provider": provider.name}
            else:
                note_inbox = {}
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            # A note that the check moved it back no longer applies.
            note = {"error": ""} if (item.get("error") or "").startswith(MOVED_BACK_PREFIXES) else {}
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body,
                message_id=result.message_id, thread_ref=result.thread_ref, **note,
                **note_inbox,
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from sellbird.models.conversation import Message
                        convo.thread.append(
                            Message(sender="sellbird", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            details = {
                "to": item["to_email"], "step": item["step"],
                "kind": item["kind"], "provider": provider.name,
            }
            if inbox is not None:
                details["from"] = inbox.address
            if receipt_id:
                details["precleared_receipt"] = receipt_id
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details=details,
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {provider.name}"
                + (f" from {inbox.address}." if inbox is not None else ".")
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        self.compliance_held = compliance_paused
        for reason, count in inbox_waits.items():
            logger.info(f"Sender: {count} email(s) wait: {reason}.")
        if outside_window:
            logger.info(
                f"Sender: {outside_window} cold email(s) wait for the recipient's "
                f"send window ({window.start}-{window.end} their time).")
        if unknown_held:
            logger.info(
                f"Sender: {unknown_held} cold email(s) wait on a fact Precleared couldn't "
                f"establish; the Outbox says what would settle each one.")
        if self.precleared is not None:
            if hold is not None:
                # The page read above stops at a few times the cycle's
                # budget; count every cold email still waiting on the check:
                # due with no stored verdict to go on, or moved back by it.
                waiting_on_check = max(waiting_on_check, await self.state.count_due_cold_outbox(
                    now, has_cached_verdict=self._has_cached_verdict,
                    moved_back_prefix=RECHECK_PREFIX))
                await record_hold(self.state, hold, waiting_on_check)
            elif answered:
                await clear_hold(self.state)
        if held_cold and inboxes is not None:
            logger.info(f"Sender: {held_cold} cold email(s) wait for their inbox's daily cap. "
                        "Replies still go out. See `sellbird inboxes list`.")
        elif held_cold:
            warmup = getattr(self.config.channels.email, "warmup", None)
            days = warmup.days_in() if warmup is not None else None
            if days is not None and cold_cap < max_daily:
                logger.info(
                    f"Sender: warmup day {days}: {held_cold} cold email(s) wait; today's "
                    f"cold cap is {cold_cap}. Replies still go out. See `sellbird warmup`.")
            else:
                logger.info(f"Sender: cold email cap reached for today ({cold_cap}).")
        if sent_cold:
            await self.state.remember_footer_lines(compliance_footer_lines(self.config))
            notice = compliance_notice(self.config)
            if notice:
                logger.warning(f"Sender: {notice}")
        if sent:
            logger.info(f"Sender: {sent} email(s) sent this cycle "
                        f"({sent_today + sent}/{max_daily} today).")


    # ── WhatsApp (channels.whatsapp, through Wasi) ────────────────────────────
    #
    # A separate drain, on purpose. It shares everything that is about reaching a
    # person — the outbox ladder, _blocked (suppressions, stop-on-reply, the gate,
    # autopilot review), claim-then-send, the daily cap, the kill switch — and
    # none of what is about email: no Precleared check, no inbox routing, no
    # signature, no warmup ramp, no CAN-SPAM footer, no follow-up spacing.
    # Threading WhatsApp through _drain_due would have meant a dozen more
    # `if channel == "email"` branches in the most load-bearing function in the
    # repo. This is shorter and cannot change how email behaves.


    async def _whatsapp_budget(self) -> tuple[int, str]:
        """(how many may go now, why not) for this cycle."""
        whatsapp = self.config.channels.whatsapp
        sent_today = await self.state.count_outbox_sent_today(channel="whatsapp")
        budget = min(MAX_SENDS_PER_CYCLE, whatsapp.max_daily_sends - sent_today)
        if budget <= 0:
            return 0, (f"today's WhatsApp cap is used up "
                       f"({sent_today}/{whatsapp.max_daily_sends})")
        # Meta's own allowance for the next 24 hours beats any local guess.
        try:
            remaining = await self.whatsapp.remaining_today()
        except Exception as e:
            # Can't read the limit: send nothing rather than risk the number.
            return 0, f"Meta's messaging limit couldn't be read ({e})"
        if remaining is not None and remaining <= 0:
            return 0, "Meta's 24-hour messaging limit for this number is used up"
        if remaining is not None:
            budget = min(budget, remaining)
        return max(budget, 0), ""

    def _template_for(self, item: dict):
        """(template, params_expressions) for a templated row, or (None, {})."""
        whatsapp = self.config.channels.whatsapp
        name = (item.get("subject") or "").strip()
        if not name:
            return None, {}
        return whatsapp.template(name), dict(whatsapp.template_params or {})

    async def _whatsapp_payload(self, item: dict, prospect) -> tuple[dict, str]:
        """What to hand the provider, or ({}, reason) when it must not go.

        A templated row is re-rendered here from the approved template and the
        current config, then compared with the text that was approved in the
        Outbox. If they differ, the config changed after a human said yes, so
        the message waits for a human again rather than quietly sending words
        nobody reviewed.
        """
        name = (item.get("subject") or "").strip()
        if not name:
            return {}, ""        # free text, inside the 24-hour window
        template, expressions = self._template_for(item)
        if template is None:
            return {}, (f"template '{name}' isn't in channels.whatsapp.templates — "
                        "run `sellbird whatsapp templates`")
        if (template.category or "").strip().lower() != "marketing" and item["kind"] == "sequence":
            # Utility templates are for messages about an existing
            # relationship. Opening a cold conversation with one is the policy
            # breach Meta punishes by re-categorising or pulling the template.
            return {}, (f"template '{name}' is a {template.category or 'non-Marketing'} "
                        "template; a cold first message needs an approved Marketing one")
        # The values as approved, when the row carries them: a parameter
        # written for this one prospect can't be recovered from config, and
        # re-rendering from config would make every such row look changed.
        params = _stored_params(item)
        if not params:
            params = {key: self._render(expression, prospect)
                      for key, expression in expressions.items()}
        for key, value in sorted(params.items()):
            problem = template_param_problem(value)
            if problem:
                return {}, f"the value for {key} can't be sent: {problem}"
        missing = [p for p in template_placeholders(template.body) if not params.get(p)]
        if missing:
            return {}, (f"template '{name}' needs {', '.join(missing)} — add them to "
                        "channels.whatsapp.template_params")
        rendered = render_template(template.body, params)
        if _collapse(rendered) != _collapse(item["body"]):
            return {}, ("the template or its parameters changed after this was "
                        "approved, so it no longer matches what you read")
        problem = template_mismatch(rendered, template.body)
        if problem:
            return {}, problem
        return {"template": template.name, "params": params}, ""

    async def _whatsapp_params_for(self, prospect, template) -> tuple[dict, str]:
        """Every parameter one template send needs, or ({}, why not).

        Placeholders listed in ``channels.whatsapp.ai_params`` are written for
        this prospect by the Writer; the rest come from the configured
        expressions. Whoever wrote a value, it is checked against Meta's
        parameter rules here: a rejected template send costs the number's
        standing, so this fails closed and says which value.
        """
        whatsapp = self.config.channels.whatsapp
        placeholders = template_placeholders(template.body)
        params = {key: self._render(expression, prospect)
                  for key, expression in (whatsapp.template_params or {}).items()}
        wanted = [p for p in placeholders if p in whatsapp.ai_params]
        if wanted and self.brain is not None:
            from sellbird.agents.writer import Writer
            try:
                written = await Writer(
                    self.brain, self.state, self.config, self.env
                ).write_whatsapp_params(prospect, template, wanted)
            except Exception as e:
                # The configured expressions still stand; say so and go on.
                logger.warning(f"Sender: couldn't write WhatsApp parameters for "
                               f"{prospect.company or prospect.full_name()} ({e}) — "
                               "using channels.whatsapp.template_params.")
                written = {}
            params.update(written)
        for key, value in sorted(params.items()):
            problem = template_param_problem(value)
            if problem:
                return {}, f"the value for {key} can't be sent: {problem}"
        missing = [p for p in placeholders if not params.get(p)]
        if missing:
            return {}, (f"template '{template.name}' needs {', '.join(missing)} — add "
                        "them to channels.whatsapp.template_params or "
                        "channels.whatsapp.ai_params")
        return params, ""

    async def stage_whatsapp_opener(self, prospect_id: str) -> tuple[str, str]:
        """Queue a cold WhatsApp sequence for a prospect. (step 1's row id, why not).

        The opener and every ``channels.whatsapp.follow_ups`` step, each at its
        own delay. Queued, not sent: they join the Outbox at pending_review
        like anything else, and the drain holds a follow-up until the step
        before it has actually gone out.

        Deliberately one prospect at a time, called by the CLI or the
        dashboard — WhatsApp openers do not ride along with email campaigns,
        because contacting someone on two channels at once is a decision the
        user makes, not a side effect of turning the channel on.

        Nothing is queued unless every step can be: half a sequence on
        someone's phone is worse than none.
        """
        whatsapp = self.config.channels.whatsapp
        if not whatsapp.enabled:
            return "", "the WhatsApp channel is off (channels.whatsapp.enabled)"
        if not whatsapp.cold_first_touch:
            return "", ("cold first contact on WhatsApp is off "
                        "(channels.whatsapp.cold_first_touch)")
        if self.whatsapp is None:
            return "", "WASI_API_KEY is not set in .env"
        prospect = await self.state.get_prospect(prospect_id)
        if prospect is None:
            return "", "no such prospect"
        number = prospect.phone_e164
        if not number:
            # Normalised here for the first time: it must be stored, not just
            # used. An inbound message is matched on phone_e164, so sending to
            # a number the record doesn't carry makes the reply unattributable
            # — and an unattributable reply is an opt-out that never lands.
            number = await self.state.set_prospect_phone(
                prospect.id, prospect.phone, whatsapp.default_country_code)
        if not number:
            return "", (f"no usable phone number for {prospect.full_name()} "
                        f"({prospect.phone or 'none on file'})")
        if prospect.status not in SENDABLE_STATUSES:
            return "", f"prospect status '{prospect.status}' is not sendable"
        suppressed = await self.state.is_suppressed(number)
        if suppressed:
            return "", f"suppressed ({suppressed})"
        # Every step rendered and checked before anything is queued.
        planned = []
        waited = 0
        for step, name, delay_days in whatsapp.sequence():
            template = whatsapp.template(name)
            if template is None:
                where = ("channels.whatsapp.cold_template" if step == 1
                         else f"channels.whatsapp.follow_ups step {step}")
                return "", (f"template '{name}' ({where}) isn't in "
                            "channels.whatsapp.templates — run `sellbird whatsapp templates`")
            params, reason = await self._whatsapp_params_for(prospect, template)
            if reason:
                return "", reason
            body = render_template(template.body, params)
            problem = template_mismatch(body, template.body)
            if problem:
                return "", problem
            waited += max(0, delay_days)
            planned.append((step, template.name, body, params, waited))

        await sync_sending_mode(self.config, self.state)
        require_approval = whatsapp.require_approval
        now = _utcnow()
        first_id = ""
        for step, name, body, params, waited in planned:
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                step=step,
                to_email=number,
                # WhatsApp has no subject, so the column carries which approved
                # template this is. The drain re-renders it and checks the match.
                subject=name,
                body=body,
                params_json=json.dumps(params, ensure_ascii=False),
                send_at=(now + timedelta(days=waited)).isoformat(),
                status="pending_review" if require_approval else "approved",
                channel="whatsapp",
                provider=self.whatsapp.name,
                approved_by="" if require_approval else "autopilot",
            )
            if step == 1:
                if not item_id:
                    return "", "already queued"
                first_id = item_id
        await self.state.queue_prospect_if_unsent(prospect.id)
        return first_id, ""

    async def _drain_whatsapp(self):
        """Send due, approved WhatsApp rows through Wasi."""
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(f"Sender: WhatsApp held, sending is paused ({paused}).")
            return
        whatsapp = self.config.channels.whatsapp
        if whatsapp.pause_below_green:
            try:
                problem = await self.whatsapp.quality_problem()
            except Exception as e:
                logger.warning(f"Sender: couldn't read the WhatsApp number's standing ({e}) "
                               "— holding WhatsApp this cycle.")
                return
            if problem:
                logger.error(f"Sender: no WhatsApp sent — {problem}. "
                             "Fix the cause before sending more.")
                await self.state.note_sends_held(f"WhatsApp: {problem}")
                return
        if self.cold_only and whatsapp.send_window is None:
            # Quiet hours. The heartbeat only reaches the Sender at all because
            # cold EMAIL is due in a recipient's own window; WhatsApp has no
            # window configured, so sending now would be the operator's 3am.
            logger.info("Sender: quiet hours and channels.whatsapp.send_window is "
                        "not set — WhatsApp waits for the morning.")
            return
        budget, why_not = await self._whatsapp_budget()
        if budget <= 0:
            logger.info(f"Sender: no WhatsApp this cycle — {why_not}.")
            return

        now = _utcnow().isoformat()
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=budget * 3, channel="whatsapp")
        if not due:
            return
        window = whatsapp.send_window
        sent = 0
        outside_window = 0
        for item in due:
            if sent >= budget:
                break
            if self.cold_only and item["kind"] != "sequence":
                continue
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky=False)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            # Step 2 never goes before step 1 has, and is dropped when step 1
            # never will. Same rule, same code, as an email follow-up.
            order = await self._follow_up_order(item, prospect)
            if order == "wait":
                continue
            if isinstance(order, tuple):
                await self.state.update_outbox_item(
                    item["id"], status=order[0], error=order[1])
                continue
            if (window is not None and item["kind"] == "sequence"
                    and await self._outside_window(
                        item, await self._zone_or_default(prospect), window)):
                outside_window += 1
                continue
            payload, reason = await self._whatsapp_payload(item, prospect)
            if reason:
                logger.warning(f"Sender: holding WhatsApp to {item['to_email']}: {reason}")
                await self.state.update_outbox_item(
                    item["id"], status="pending_review",
                    error=(HELD_FOR_REVIEW + reason)[:300])
                continue
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            item = await self.state.get_outbox_item(item["id"]) or item
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky=False)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            try:
                result = await self.whatsapp.send_email(
                    item["to_email"], "", item["body"],
                    thread_ref=item.get("thread_ref", ""), **payload)
            except Exception as e:
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"{MAYBE_SENT_PREFIX}WhatsApp send error: {e}"[:300])
                logger.warning(f"Sender: WhatsApp send to {item['to_email']} raised: {e}")
                continue
            if not result.ok:
                await self._whatsapp_failed(item, prospect, result)
                continue
            sent += 1
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=_utcnow().isoformat(),
                message_id=result.message_id, thread_ref=result.thread_ref, error="")
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")
            await self.state.log_action(
                action_type="whatsapp_sent", agent="sender",
                details={"to": item["to_email"], "kind": item["kind"],
                         "template": payload.get("template", ""),
                         "step": item["step"]},
            )
            logger.info(f"Sender: WhatsApp sent to {item['to_email']}"
                        + (f" (template {payload['template']})" if payload.get("template") else ""))
            if self.send_pacing and sent < budget:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if outside_window:
            logger.info(f"Sender: {outside_window} WhatsApp message(s) wait for the "
                        f"recipient's send window.")
        if sent:
            logger.info(f"Sender: {sent} WhatsApp message(s) sent this cycle.")

    async def _whatsapp_failed(self, item: dict, prospect, result) -> None:
        """A send Wasi or Meta refused."""
        error = result.error or "the send failed"
        if getattr(result, "error_code", "") == WINDOW_CLOSED:
            # Not a failure of this message: it needs to be a template, or to
            # wait until the person writes again. Keep it, say why.
            await self.state.update_outbox_item(
                item["id"], status="pending_review",
                error=(HELD_FOR_REVIEW + "WhatsApp only allows free text within 24 "
                       "hours of their last message. Send an approved template "
                       "instead, or wait for them to write.")[:300])
            logger.info(f"Sender: {item['to_email']} is outside the 24-hour window.")
            return
        # Meta's own numeric code, not the message text: Meta puts the code in
        # the message for some failures ("(#131009) ...") and not others
        # ("Message undeliverable" is 131026), so a substring match silently
        # missed half the dead numbers and never suppressed them.
        if getattr(result, "upstream_code", None) in DEAD_NUMBER_META_CODES:
            # WhatsApp's hard bounce: this number will never receive anything.
            await self.state.update_outbox_item(
                item["id"], status="failed", error=error[:300])
            if prospect is not None:
                # The number stays on the record, and keeps its E.164 form, so
                # the suppression below can still be matched to this person.
                # Passing no default_code would have blanked phone_e164 for any
                # number that only normalises with one.
                await self.state.set_prospect_phone(
                    prospect.id, prospect.phone,
                    self.config.channels.whatsapp.default_country_code,
                    verified=False)
                await self.state.add_suppression(
                    "phone", item["to_email"], reason="bounced", source="whatsapp")
                cancelled = await self.state.cancel_pending_outbox_for_prospect(
                    prospect.id, reason="whatsapp number unreachable")
                logger.warning(
                    f"Sender: {item['to_email']} is not reachable on WhatsApp — "
                    f"suppressed, {cancelled} queued message(s) cancelled. Email is "
                    "unaffected.")
            return
        prefix = MAYBE_SENT_PREFIX if getattr(result, "uncertain", False) else ""
        await self.state.update_outbox_item(
            item["id"], status="failed", error=f"{prefix}{error}"[:300])
        logger.warning(f"Sender: WhatsApp to {item['to_email']} failed: {error}")

def _names(prospect) -> list[str]:
    """The prospect's own name and company: never read as an AI tell."""
    if prospect is None:
        return []
    return [n for n in (prospect.company, prospect.first_name, prospect.last_name) if n]


def _stored_params(item: dict) -> dict:
    """A WhatsApp row's template parameters as they were approved ({} when
    the row has none: every row queued before params_json existed)."""
    raw = (item.get("params_json") or "").strip()
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {str(k): str(v) for k, v in stored.items() if str(v or "").strip()}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
