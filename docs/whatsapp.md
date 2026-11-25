# WhatsApp (through Wasi)

Sellbird can reach a prospect on WhatsApp as well as by email, and keep a
conversation going in whichever channel they answered in. Messages go through
[Wasi](https://wasi.sirahagents.com), a WhatsApp CRM on Meta's official Cloud API.

**The channel is off by default and nothing here runs until you turn it on.**

---

## Read this before you turn it on

WhatsApp is not email, and the difference is not a detail:

- **You cannot just message someone.** Meta delivers a first message only as a
  **template it has approved in advance**. Free text is allowed only inside 24 hours
  of the person's own last message.
- **Templates have categories, and they matter.** A cold first message needs an
  approved **Marketing** template. Using a *Utility* template (an invoice, a booking
  confirmation, a reminder) for outreach is a policy breach Meta punishes by
  re-categorising the template or pulling it.
- **Blocks cost you the number.** People block a business on WhatsApp far more
  readily than they mark an email as spam, and enough blocks take the number — and
  the WhatsApp Business Account behind it — with them.

Sellbird will not stop you doing cold WhatsApp outreach, but it makes it slow and
visible: template-only cold sends, verified against the approved text, under Meta's
own limit, with every send logged and a stop the moment the number's quality rating
drops.

If you want one recommendation: use WhatsApp for people who **already replied** to
an email and gave you a number. Reply rates there are excellent and nothing is at
risk.

---

## Setup

### 1. A Wasi account with the number connected

You need a Wasi workspace with a WhatsApp number connected (Wasi walks you through
Meta's Embedded Signup). Then get an API key from Wasi and put it in `.env`:

```
WASI_API_KEY=
# Only if you self-host Wasi; the default is https://wasi.sirahagents.com
WASI_API_BASE_URL=
```

The key is an `.env` value only. It never goes in a config file, never in the
database, and it is masked in the log.

### 2. Turn the channel on

In the config file Sellbird reads (`sellbird.local.yaml` if you have one, else
`sellbird.yaml`):

```yaml
channels:
  whatsapp:
    enabled: true
    # A dialling prefix, used for numbers written the way they'd be dialled
    # locally ("020 7946 0018"). A prefix, not a country code.
    default_country_code: "+44"
```

Check it:

```bash
sellbird whatsapp test
```

That prints whether the number is connected, Meta's quality rating for it, and how
much of Meta's 24-hour allowance is left.

### 3. Normalise the numbers you already have

Discovery already collects company phone numbers. Put them in the form WhatsApp
needs:

```bash
sellbird whatsapp backfill-phones
```

### 4. Copy the templates you'll use

```bash
sellbird whatsapp templates
```

That lists what Meta has approved, with each template's category and placeholders.
Copy the ones you want Sellbird to send into your config, and say how each
placeholder is filled:

```yaml
channels:
  whatsapp:
    templates:
      - name: cold_opener
        category: Marketing
        language: en_US
        body: "Hi {{customer_name}}, saw {{practice_name}} has no online booking."
    template_params:
      customer_name: "{{first_name}}"
      practice_name: "{{company}}"
```

The left side is Meta's placeholder name; the right side uses the same merge
variables as an email body (`{{first_name}}`, `{{last_name}}`, `{{company}}`,
`{{title}}`, `{{personalization}}`).

Sellbird keeps this copy so it can check, before sending, that the text it rendered
really is that template with its placeholders filled. Without a stored copy a
template send is refused.

### 5. Cold first contact (optional, and the risky one)

```yaml
channels:
  whatsapp:
    cold_first_touch: true
    cold_template: cold_opener      # must be an approved Marketing template
    max_daily_sends: 20
```

Then queue one, for one prospect:

```bash
sellbird whatsapp queue --prospect <id>
sellbird outbox --channel whatsapp     # read it, then approve
```

Openers are queued one at a time, on purpose. They do **not** ride along with email
campaigns: messaging someone on two channels at once is your decision, not a side
effect of enabling the channel.

### 6. Let Claude write the parts that change

An approved template's words are fixed -- that is what Meta approved. The
placeholder *values* are the only thing that can differ per person, so those are
worth writing properly:

```yaml
channels:
  whatsapp:
    ai_params: [practice_name]        # Claude writes these, per prospect
    template_params:
      customer_name: "{{first_name}}"  # the rest stay as merge variables
      practice_name: "{{company}}"     # also the fallback for the above
```

Claude gets the same facts it writes a first email from -- the company's own
site, its signals, the research notes -- and must stay inside them. What it
writes is checked before it is queued: Meta rejects a parameter containing a
line break, a tab or more than four spaces in a row, and a rejected template
send costs the number's standing. Anything that fails, or a model that is
unavailable, falls back to the `template_params` expression above, so a bad
answer costs personalisation and never the message.

The value a human approves in the Outbox is the exact value that goes to Meta.
Change the config after approving and the message waits for a human again.

### 7. Follow-ups

Someone who never replied is still outside the 24-hour window, so each
follow-up is another template send:

```yaml
channels:
  whatsapp:
    follow_ups:
      - template: nudge             # another approved *Marketing* template
        delay_days: 3
      - template: breakup
        delay_days: 4               # 4 days after the nudge, 7 after the opener
```

`sellbird whatsapp queue` then stages the whole sequence at once, each step at
its own delay. The rules are email's, in the same code:

- step 2 never goes out before step 1 actually has, whatever their scheduled
  times say -- approving a batch late can't fire a whole sequence at once;
- a step whose predecessor ended as anything but `sent` is cancelled, with the
  reason on the row;
- a reply stops every queued step, on **both** channels;
- nothing is staged unless every step can be: half a sequence on someone's
  phone is worse than none.

With no `follow_ups`, it sends one message and stops.

### Seeing where you stand

Dashboard -> **Settings -> WhatsApp** shows the number, Meta's live quality
rating and remaining 24-hour allowance, your own cap and what you've used
today, and the sequence step by step with each template's category -- so a
Utility template sitting in a cold sequence is visible before you send it,
not after Meta reacts. It is read-only: turning the channel on, and cold
first contact especially, stays a config-file decision.

---

## What Sellbird does and doesn't do

**Shared with email, because it is about reaching a person, not about a medium:**
the Outbox approval ladder, the deterministic pre-send gate, the do-not-contact
list, suppressions, stop-on-reply (a reply on either channel cancels queued
messages on both), conversations, the kill switch (`sellbird sending pause`), and
intent classification.

**WhatsApp's own:**

| | |
|---|---|
| Daily cap | the lower of `channels.whatsapp.max_daily_sends` and Meta's `remaining` |
| Quality | a rating below `GREEN` stops cold sending (`pause_below_green`) |
| 24-hour window | enforced by Wasi; a closed window holds the row and says so |
| Opt-out | Meta's Block button, plus the usual "stop"/"unsubscribe" words |
| Recipient | an E.164 number, validated before anything is queued |
| Length | capped well under Meta's 4096, because a wall of text gets you blocked |

**Not done for email:** no CAN-SPAM postal footer (Meta enforces opt-out itself), no
Precleared check, no warmup ramp (Meta's own tier is the ramp), no signature logo.

---

## Opt-outs cross channels

Saying stop on one channel is not permission to use the other:

- "stop" on WhatsApp suppresses the phone **and** the email address;
- an email unsubscribe suppresses the address **and** their number;
- a WhatsApp **block** suppresses the number only — a block in one app is not an
  unsubscribe from the other, and Sellbird will not infer one.

A block or an in-WhatsApp opt-out sends no message Sellbird could read, so it reads
Wasi's own contact state each cycle to catch them.

---

## A number that doesn't work

WhatsApp has no bounce message. A number that isn't on WhatsApp shows up as an
outbound message whose status goes to `failed`, sometimes minutes after the send was
accepted. Either way Sellbird treats it as email treats a hard bounce: the number is
suppressed, queued WhatsApp for that person is cancelled, and **their email sequence
is untouched**.

---

## Troubleshooting

**"outside the 24-hour window"** — they haven't messaged you in 24 hours, so free
text isn't allowed. Send an approved template, or wait for them to write.

**"template X isn't in channels.whatsapp.templates"** — run
`sellbird whatsapp templates` and copy it into your config.

**"template X is a Utility template; a cold first message needs an approved
Marketing one"** — exactly that. Get a Marketing template approved in Wasi.

**"the template or its parameters changed after this was approved"** — you edited the
template or its parameters after a human approved that message. It waits for a human
again rather than sending words nobody read.

**"Meta's quality rating for this number is YELLOW"** — people are blocking or
reporting you. Cold sending has stopped. Fix the targeting or the message before
sending more; this is the last warning before Meta restricts the number.

**Nothing sends and the log says the messaging limit couldn't be read** — Sellbird
sends nothing rather than guess at Meta's allowance. Check `sellbird whatsapp test`.
