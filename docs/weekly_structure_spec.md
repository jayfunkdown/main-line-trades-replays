# Weekly Gain / Loss Structure Spec

This is the V1 detector spec for the whole-market weekly screener.
The classifier in `scripts/weekly_screener.py` must follow these rules only.

Trained from Jason’s charts: LNSR 6.42, MH 12.21, CRMD 7.13, PODC, POWERGRID,
BSX, SGA, and NFLX 77.65.

If later training disagrees with a rule below, change this spec first, then
the fixtures, then the classifier. Do not special-case live tickers.

## What “the weekly” is

It is **not** last week’s high or low.

1. Find the **swing low** of the current range (the low of the move).
2. That low must be a **new low**. If a later dip does **not** make a new
   low, do **not** move the weekly. Intermediate candles (the MH “X”) are
   not the weekly.
3. If it **does** make a new low, the weekly **re-anchors** to the origin
   of **that** drop.
4. The origin is the **weekly swing whose move made that low** — the top
   of the containment. Draw the line on that candle’s **body**
   (`max(open, close)`), never the wick.
5. **Gained the weekly** = a later weekly candle **closes through** that
   body line.

LNSR: new low ~5, origin body **6.42**, this week through it = stage 1.  
MH: actual low is the small-circle low (not an earlier false low). Origin
body **12.21**. The X is not the weekly because it did not make that low.  
If MH had kept dropping to a **new** low from the X candle, the weekly
would have moved to that X candle’s body.

## Two stages

**Stage 1 — gained, watchlist**  
Price has closed through the origin body and is **more than 1%** away
(LNSR 8.27 vs 6.42, PODC still above 2.43). Do not post to Signals.

**Stage 2 — send to Signals**  
Last price is within **1%** of that same body:

`abs(last - level) / level <= 0.01`

This includes:

- A later pullback/retest into the line (CRMD wick into 7.13, POWERGRID
  sitting on the orange line), **or**
- The **same week it was gained**, if the close is still within 1% of the
  body (NFLX 78.16 vs 77.65). Do **not** wait for a deeper retest in that
  case.

**Lost the weekly** is the **exact inverse** (flip the chart):

1. Find the **swing high** of the range (the high of the move).
2. That high must be a **new high**. If a later bounce does **not** make a
   new high, do **not** move the weekly.
3. If it **does** make a new high, re-anchor to the origin of **that** rally.
4. Draw the line on that origin candle’s **body** (`min(open, close)`),
   never the wick.
5. **Lost the weekly** = a later weekly candle **closes through** that
   body line (below it).

Stage 1 / stage 2 and the **1%** rule are the same. Same-week close still
inside 1% sends immediately.

## Scan vs watch

- **Scan:** only after that market’s weekly has **closed**.
  - **US:** Friday 4:00 PM America/New_York through Sunday.
  - **Crypto:** Monday 00:00 UTC (after the Sunday UTC weekly print),
    using the last **closed** week — not a forming Monday candle.
  Hourly batches. Admit names that have gained or lost the weekly. No
  Discord post unless stage 2 is already true on the later watch pass.
- **Watch:** every 15 minutes, watchlist only. Post when last price is
  within 1% of the watched body.

## Exclusions

- Crypto stables and wrapped assets (list in the screener).
- US names below min price / average volume.
- Warrants, units, preferred-style suffixes.
- Fewer than enough weekly candles to find a swing + origin.
- One Discord post per `symbol + side + level`.
- Drop a watch after **8** ISO weeks with no 1% touch.

## Discord

Public beta `# 📈 | weekly-retest-beta`. Neon pink cards. Same Signals
layout: **# 📈 Trade Signal**, Long/Short, then **## 🧠 Trade Thesis**
with **Weekly gained** or **Weekly lost**. This is the first automated
Signals feed; later scanners should reuse this card. Daily cap remains.

Do not enable timers until `--preview` is clean and state is seeded.
