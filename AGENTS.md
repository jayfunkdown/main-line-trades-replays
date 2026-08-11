# Main Line Trades — Codex Project Instructions

## Project Purpose

Main Line Trades is a live Discord trading community automation system.

The project publishes and manages:

- Morning Briefs
- Weekly U.S. Economic Calendars
- Earnings reactions
- Private earnings review posts
- Send-to-Signals workflow
- TrendSpider chart posts
- Truth Social / Trump posts
- YouTube replay notifications
- Discord moderation/supporting automation

This repository is connected to a LIVE PRODUCTION Discord server.

Treat all changes as production-sensitive.

---

# 1. Core Rule: Inspect Before Changing

Before changing any code:

1. Inspect the existing implementation.
2. Search for related functions, state files, environment variables, workflows, and service definitions.
3. Understand how the feature is currently deployed.
4. Explain the proposed change.
5. Make the smallest necessary modification.
6. Test locally/safely.
7. Show the diff.
8. Do NOT deploy to production without explicit approval.

Do not rebuild working features simply because another implementation is possible.

Preserve existing behavior unless the requested task explicitly changes it.

---

# 2. Production Environment

Production project path:

    /home/jason/main-line-trades-replays

Python virtual environment:

    /home/jason/main-line-trades-replays/.venv

Production Linux user:

    jason

Primary Git branch:

    main

Production scheduling is handled by systemd.

Backed-up unit files are located in:

    deploy/systemd/

Actual production systemd files are installed under:

    /etc/systemd/system/

---

# 3. Important Scripts

## Earnings

    scripts/earnings_reactions.py

Responsibilities:

- Fetch earnings data
- Filter completed earnings
- Rank candidates
- Fetch price movement
- Generate earnings review posts
- Generate public earnings reaction posts
- Generate weekly charts
- Handle Send to Signals workflow
- Run the persistent Earnings Review Discord bot

Important modes include:

    --post
    --preview
    --review-bot
    --private-test
    --date
    --limit
    --preview-limit
    --force

Do NOT use --force unless there is a specific approved reason.

---

## Morning Brief

    scripts/morning_brief.py

Posts the weekday Morning Brief.

Production schedule:

    Monday-Friday
    6:05 AM America/New_York

---

## Weekly Economic Calendar

    scripts/weekly_economic_calendar.py

Posts one weekly U.S. economic calendar.

Production schedule:

    Monday
    6:00 AM America/New_York

This is intentionally separate from:

    scripts/economic_calendar.py

Do NOT replace the weekly calendar with the older daily economic calendar behavior.

---

## TrendSpider

    scripts/trendspider_filter.py

Reads raw TrendSpider Discord content and forwards qualifying chart posts.

Production mode requires:

    --post

Production polling:

    Every 1 minute

Processed state:

    data/trendspider_processed.json

---

## Truth Social / Trump

    scripts/trump_filter.py

Reads raw Truth Social / Trump content and publishes qualifying posts.

Safe preview mode:

    --preview

Production mode:

    --post

Production polling:

    Every 1 minute

Processed state:

    data/trump_processed.json

Historical backlog was deliberately seeded as processed before production was enabled.

Do not reset this state casually.

---

## YouTube Replays

    scripts/replay_to_discord.py

Posts eligible YouTube replay notifications.

Safe preview:

    --preview

Production mode:

    --post

Production polling:

    Every 5 minutes

Processed state:

    data/posted_ids.json

Historical replay IDs were deliberately seeded before enabling production.

## YouTube Training Videos

    scripts/replay_to_discord.py --post --feed training

Watches the configured Main Line Trades training playlist and posts newly added
tutorials with the same bordered-card presentation as replay notifications.

Production polling:

    Every 30 minutes

Processed state:

    data/training_posted_ids.json

The initial playlist import is explicit and uses `--import-existing`; normal
timer runs never backfill an empty state.

## YouTube Video Intel

    scripts/replay_to_discord.py --post --feed video-intel

Watches the three approved external YouTube channels and publishes newly added
videos in the branded Video Intel card format.

Production polling:

    Every 30 minutes

Processed state:

    data/video_intel_posted_ids.json

An empty state is seeded from current uploads without posting. Historical
videos must never be backfilled automatically.

---

# 4. Production Services and Timers

Persistent service:

    mainline-earnings-review.service

This runs continuously and handles the Earnings Review / Send to Signals Discord workflow.

Scheduled units:

    mainline-earnings-post.service
    mainline-earnings-post.timer

    mainline-morning-brief.service
    mainline-morning-brief.timer

    mainline-weekly-calendar.service
    mainline-weekly-calendar.timer

    mainline-trendspider.service
    mainline-trendspider.timer

    mainline-trump-filter.service
    mainline-trump-filter.timer

    mainline-youtube-replays.service
    mainline-youtube-replays.timer

    mainline-youtube-training.service
    mainline-youtube-training.timer

    mainline-youtube-video-intel.service
    mainline-youtube-video-intel.timer

Copies of these production units are stored in:

    deploy/systemd/

---

# 5. Current Production Schedule

## Earnings

Monday-Friday:

    6:00 AM America/New_York

The production earnings service MUST currently include:

    EARNINGS_EARLY_MORNING_CUTOFF_HOUR=7

Reason:

The earnings job runs at 6:00 AM Eastern.

The original automatic target-date logic used:

    now_eastern.hour < cutoff_hour

With a cutoff of 6, a job executing exactly at 6:00 AM selected the CURRENT day rather than the previous trading day.

That caused Finnhub to return zero completed earnings reports.

The service-level cutoff was changed to 7 so the 6:00 AM job selects the previous U.S. trading day.

Do not remove or change this without addressing the underlying date-selection behavior and testing it.

---

## Morning Brief

Monday-Friday:

    6:05 AM America/New_York

This is intentionally staggered five minutes after the 6:00 AM earnings and
weekly-calendar jobs to avoid concurrent API work.

---

## Weekly Economic Calendar

Monday:

    6:00 AM America/New_York

---

## TrendSpider

Every:

    1 minute

---

## Truth Social / Trump

Every:

    1 minute

---

## YouTube Replays

Every:

    5 minutes

---

# 6. Runtime State Files — DO NOT CASUALLY MODIFY

These files are production runtime state:

    data/earnings_reactions_state.json
    data/earnings_calendar_cache.json
    data/trendspider_processed.json
    data/trump_processed.json
    data/posted_ids.json
    data/training_posted_ids.json
    data/video_intel_posted_ids.json

Generated earnings charts are also runtime output:

    data/earnings_charts/

These files may appear modified in git status during normal operation.

Do NOT:

- reset them
- delete them
- overwrite them
- stage them automatically
- commit them without understanding why
- regenerate them unnecessarily

They protect the Discord server from duplicate or historical posts.

---

# 7. Backlog Safety

Before enabling a new posting automation:

1. Inspect its state logic.
2. Run preview mode if available.
3. Determine whether old messages/items are waiting.
4. Do not enable production posting if a historical backlog exists.
5. Back up the state file.
6. Mark historical source IDs as processed without posting them.
7. Preview again.
8. Require a clean/new-only state.
9. Enable production.
10. Inspect systemd logs after the first run.

This exact process was used for Truth Social and YouTube.

---

# 7A. Discord Presentation Standard

Every polished post generated by Main Line Trades must use a bordered Discord
embed card. This is a server-wide standard, not a channel-specific preference.

Use the shared brand palette from `scripts/discord_embeds.py`:

- Electric blue (`#00CFFF`) for informational posts, calendars, charts,
  replays, and market summaries.
- Neon pink (`#FF2BD6`) for action-oriented posts such as Signals, Earnings
  Movers, and live-session alerts.

Preserve each feed's approved wording and internal layout unless a task
explicitly requests a content redesign. Raw intake channels, internal bot logs,
ephemeral interaction responses, and normal member messages do not require
embed cards.

The bordered card is a wrapper around the approved presentation, not a reason
to compress or redesign it. When converting an existing polished post, preserve
the original Markdown hierarchy, large headings, blank-line spacing, emojis,
dividers, wording, disclaimers, and chart placement inside the embed description.
Avoid replacing that layout with tightly stacked embed fields unless the user
explicitly approves a compact field-based design.

The Training Videos and Past Live Streams card layouts are already approved.
Do not redesign or migrate those two feeds unless the user makes a new explicit
request for them.

New public or staff-facing publishing features must inherit this presentation
standard by default.

---

# 8. Discord Safety

Never flood Discord during testing.

Use:

- preview modes
- explicit limits
- test channels where appropriate
- one controlled post before enabling automation

Do not manually run production posting commands repeatedly.

Persistent state should be relied upon for duplicate prevention.

Do not assume deleting a Discord message resets bot state.

Discord messages and runtime state are separate.

---

# 9. Earnings Workflow

The earnings system has two main outputs.

## earnings-reactions

Public/high-priority earnings reactions.

## earnings-review

Broader private review queue.

The review workflow contains a:

    Send to Signals

interaction/button.

The persistent Earnings Review bot handles this interaction.

The workflow has been tested successfully end-to-end on production.

---

# 10. Current Requested Feature

Next requested feature:

    /clear-earnings-review

This should be a Discord slash command implemented in the EXISTING Earnings Review bot.

Desired behavior:

- Run directly from Discord
- Staff/owner/admin authorized only
- Operate only on the earnings-review channel
- Quickly remove old/handled review posts
- Avoid deleting pinned messages
- Avoid deleting Discord system messages
- Avoid deleting unrelated human content
- Prefer targeting bot-generated earnings review messages
- Report how many messages were removed
- Avoid leaving unnecessary permanent command-response clutter

Before implementation:

1. Inspect existing Discord bot initialization.
2. Inspect interaction/slash-command framework.
3. Inspect Send to Signals button implementation.
4. Inspect channel ID environment variables.
5. Inspect authorization/role logic.
6. Decide whether deletion should mean:
   - all bot review messages, or
   - handled messages only.

Do not introduce a second Discord bot for this feature unless there is a compelling architectural reason.

Use the existing:

    mainline-earnings-review.service

and existing review bot process.

---

# 11. Secrets

Never expose, print, commit, document, or hard-code:

- Discord bot tokens
- Discord webhook URLs
- Finnhub API keys
- YouTube credentials
- API tokens
- passwords
- private credentials

Environment variable names may be documented.

Secret VALUES must never be committed.

The project uses environment configuration including a project-root .env setup.

Do not display .env contents unless explicitly required and safe.

---

# 12. Git Discipline

Before making changes:

    git status --short

Never blindly use:

    git add .
    git add -A

on the production server.

Stage files explicitly.

Example:

    git add scripts/example.py

Before committing:

    git diff
    git diff --cached

Runtime state files should normally remain unstaged.

Do not reset production state merely to obtain a clean git status.

---

# 13. GitHub Actions

Several historical GitHub Actions workflows are manual-only using:

    workflow_dispatch

Production scheduling is now primarily systemd.

Before changing GitHub Actions, verify that the same task is not already running through systemd.

Do not enable both GitHub Actions scheduling and systemd scheduling for the same posting automation unless explicitly designed to avoid duplicate posts.

---

# 14. Safe Testing Order

For Python changes:

1. Read the code.
2. Make the smallest change.
3. Syntax check.

Example:

    .venv/bin/python -m py_compile scripts/example.py

4. Run unit tests if present.
5. Use preview mode if available.
6. Use one controlled production test only when approved.
7. Inspect output.
8. Inspect journal logs.
9. Show git diff.
10. Deploy only after approval.

---

# 15. Useful Production Commands

Overall running services:

    systemctl --type=service --state=running | grep mainline

Timers:

    systemctl list-timers --all | grep mainline

Failed Main Line units:

    systemctl --failed | grep mainline || true

Earnings logs:

    sudo journalctl -u mainline-earnings-post.service -n 80 --no-pager -l

TrendSpider logs:

    sudo journalctl -u mainline-trendspider.service -n 50 --no-pager

Truth Social logs:

    sudo journalctl -u mainline-trump-filter.service -n 50 --no-pager

YouTube logs:

    sudo journalctl -u mainline-youtube-replays.service -n 50 --no-pager

Earnings review service:

    systemctl status mainline-earnings-review.service --no-pager

---

# 16. systemctl Pager Note

Some systemctl commands open output in `less`.

If the bottom of the terminal shows:

    (END)

or:

    :

press:

    q

to exit the pager.

Prefer:

    --no-pager

for commands used in automation/instructions.

---

# 17. Do Not Confuse Successful Exit With Successful Work

Several project scripts are oneshot programs.

A service showing:

    Deactivated successfully

only means the program exited with status 0.

It does NOT guarantee it performed the desired operation.

Always inspect application output or journal logs.

---

# 18. Production Is the Priority

The live Discord server is more important than achieving a perfectly clean repository state.

Do not sacrifice production state or continuity merely to make:

    git status

look clean.

When uncertain:

STOP.

Inspect.

Explain the risk.

Ask for approval before making destructive or production-impacting changes.
