# Amazon.ae Stock Alert — GitHub Actions Version (no card, ever)

Runs entirely on GitHub's free servers — your laptop can be off, asleep,
wherever, and no payment card is ever required anywhere in this setup.
Checks the product page roughly every 60 seconds (configurable). The
moment it detects stock, it emails you the product link + a quick
add-to-cart link, then stays quiet until it goes out of stock and comes
back again.

Tracking: **Amazfit Helio Ring** — ASIN `B0F8HJCB47` on amazon.ae.

It never logs into Amazon, never adds anything to a cart itself, never
touches checkout — it only reads the public product page.

## Why this needs a small trick, and why the repo must be public

GitHub's scheduled triggers can't reliably fire more often than every ~5
minutes, and every single job is hard-capped at 6 hours — there's no
setting for "just run forever." So instead of one continuous process, this
uses **overlapping shifts**:

- A trigger fires every **3 hours** (`0 */3 * * *` — 8 times a day).
- Each time it fires, the script loops internally, checking every
  `POLL_INTERVAL_SECONDS` (default 60s), for **3h50m** — which is *longer*
  than the 3-hour gap between triggers.
- That means the next shift starts about 50 minutes *before* the previous
  one finishes. For that 50-minute window, two checks are running in
  parallel. The rest of the time, exactly one is running.

Net effect: there's no moment in the day where nothing is watching the
page — the overlap is what closes the gap, not a shorter interval.

The cost: running this many overlapping ~4-hour jobs uses a lot of Actions
minutes. That's genuinely free and unlimited **only if the repo is
public** — on a private repo you'd blow past GitHub's free monthly minutes
in about a day and start being billed. Nothing sensitive lives in the code
either way (credentials are in GitHub's encrypted Secrets, which stay
private even on a public repo) — the only thing a public repo exposes is
that you're tracking this specific product, which is visible in the repo's
`ASIN` variable.

## Setup (10 minutes, one time)

1. **Create a new GitHub repo — set it to Public** — and push these files
   to it (same `git clone`/`git push` flow as your other repo).

2. **Get a Gmail App Password** (not your normal Gmail password):
   - Your Google Account must have 2-Step Verification turned on.
   - Go to `myaccount.google.com` → Security → 2-Step Verification →
     App passwords → create one (name it anything, e.g. "stock alert").
   - Google gives you a 16-character password. **Paste this directly into
     GitHub's Secrets UI in step 3 — never into a chat with me or anyone
     else.**

3. **Add repo secrets** — Settings → Secrets and variables → Actions →
   **New repository secret**, add these three:
   - `GMAIL_USER` — the Gmail address sending the alert (e.g. `you@gmail.com`)
   - `GMAIL_APP_PASSWORD` — the 16-character app password from step 2
   - `ALERT_EMAIL_TO` — where the alert should land (can be the same address)

   Secrets stay encrypted and hidden even though the repo itself is public.

4. **Add repo variables** (same Settings page, "Variables" tab):
   - `ASIN` — `B0F8HJCB47`
   - `AMAZON_DOMAIN` — `amazon.ae`
   - `POLL_INTERVAL_SECONDS` — optional, defaults to `60`. See the note
     below before pushing this lower.

5. **Enable Actions** if it's not already: Settings → Actions → General →
   allow workflows to run.

6. Go to the **Actions** tab → "Check Amazon Stock" → **Run workflow** to
   trigger it manually once and confirm it runs clean before letting the
   schedule take over.

That's it — from here it runs continuously, for free, no card anywhere in
the chain.

## How it avoids spamming you or losing state

`state.json` in the repo tracks whether you've already been notified for
the current "in stock" streak. Both overlapping runs write to it as a
best-effort commit — if two pushes collide at the same moment, one just
quietly skips (logged, not fatal) and the other wins; nothing crashes.
These frequent commits also incidentally keep the scheduled workflow from
ever being auto-disabled by GitHub's 60-day-inactivity rule.

## Things worth knowing

- **Cron timing isn't exact** — trigger points can slip a few minutes
  under GitHub load. The 50-minute overlap buffer is generous enough to
  absorb typical delays without opening a real gap.
- **Going faster than 60s isn't free.** More frequent requests raise the
  odds Amazon serves a CAPTCHA instead of a real page — which tells you
  nothing, so it can hurt reliability rather than help it. I'd be cautious
  pushing below ~30s.
- **CAPTCHA/block pages**: if one shows up, the script skips that check
  without touching your notification state, and tries again next cycle.
  If it happens constantly rather than rarely, that's worth debugging
  together — likely needs headers tweaked or the interval slowed down.
- **The quick add-to-cart link** (`.../gp/aws/cart/add.html?ASIN.1=...`) is
  a long-standing Amazon feature, not a guaranteed API — worth clicking it
  once on your first real alert to confirm it drops the item in your cart
  as expected on amazon.ae.
- To change the product later, just update the `ASIN` repo variable — no
  code change needed.
