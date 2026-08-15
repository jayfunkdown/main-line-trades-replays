# Weekly Gain / Loss — Price Action Teaching

This file is the locked teaching for how Jason finds a **weekly gain** or
**weekly loss**. Later price-action lessons should be added here (or as
sibling docs under `docs/`) the same way: write the rule in English first,
then change fixtures, then change code.

The classifier in `scripts/weekly_screener.py` must follow these rules only.
Do not special-case live tickers. If a new chart disagrees with a rule
below, change this file first.

Trained from Jason’s charts: LNSR 6.42, MH 12.21, CRMD 7.13, PODC,
POWERGRID, BSX, SGA, NFLX 77.65, AM 22.65, CGSM 26.49, FEPI 42.34,
IBDT 25.38, JNK 96.7, and IMXI 13.06.

---

## What this is not

It is **not** last week’s high or low.

Comparing this week’s close to the prior week’s wick high/low is the wrong
method. That is a different pattern and will fire on noise (tiny crypto
breaks, etc.).

The weekly is a **containment** from a swing extreme back to the **body**
of the week that made that extreme.

---

## Finding the weekly — gain / long

Read the chart as a range, then a drop, then later a close back through
the top of that drop.

1. Find the **swing low** of the current range. That is the low of the
   move — the bottom of the containment.
2. On MH, the small circle **is** that low. It is not an earlier purple
   week. There may be **no** obvious “signal candle” sitting on the low.
3. That low must be a **new low**. If a later dip does **not** take out
   that low, **do not** move the weekly.
4. Intermediate candles on the way down (the MH **X** near 10.50, the
   IMXI bounce at 14.54) are **not** the weekly. They did not make the
   low.
5. **Re-anchor rule:** if price **does** continue and print a new low,
   the weekly moves to **that later week’s body**. Hypothetical: if MH
   had kept falling from the X area into a new low (~8.30), the weekly
   would be that new-low week’s **body**, not the X. It did not, so
   **12.21 stays**.
6. The weekly line is the **body of the week that printed that low** —
   the weekly move that made the low. On IMXI that is 13.06, not the
   earlier 14.54 bounce.
7. Draw the line on that candle’s **body**:
   `max(open, close)`. **Never the wick.**
8. **Gained the weekly** = a later weekly candle **closes through** that
   body line (close strictly above it).

### Trained gain examples

| Name | Origin body (the weekly) | Notes |
| --- | --- | --- |
| LNSR | 6.42 | Body of the week that made the ~5 low. Close 8.27 is a take, still far from the line. |
| MH | 12.21 | Body of the week that made the small-circle low. X is not the weekly. |
| IMXI | 13.06 | Week of 2026-07-31 (open 13.06, low 11.15). The 14.54 bounce is not the weekly. |
| PODC | ~2.43 | Taken and still above the line. |
| NFLX | 77.65 | Just through (close 78.16). Still inside 1% the same week. |
| CRMD | 7.13 | Taken earlier; later wick/retest into the line. |
| POWERGRID | ~267.95 | Sitting on the line. |
| BSX, SGA | — | Same containment logic. |

---

## Finding the weekly — loss / short

The short side is the **exact inverse**. Flip the chart upside down; it is
the same thing.

1. Find the **swing high** of the range (the high of the move).
2. That high must be a **new high**. If a later bounce does not make a
   new high, do **not** move the weekly.
3. If it **does** make a new high, re-anchor to **that later week’s body**.
4. Draw on the body of the week that printed that high:
   `min(open, close)`. Never the wick. A bounce after the high is not
   the weekly.
5. **Lost the weekly** = a later weekly candle **closes through** that
   body line to the downside (close strictly below it).

Most names that qualify will be gained / Long. Sometimes it is lost /
Short. Both are valid.

---

## Taken vs still on the line

Gaining or losing the weekly is the **close through** the origin body.

After that, the only signal is the **first test** of that same body:

`abs(price - level) / level <= 0.01`

A wick into that 1% band counts as a visit. The take week itself only
counts if the **close** is still inside 1% (NFLX 78.16 vs 77.65). The
breakout week’s range slicing through the line is not a retest when the
close has already run away (LNSR 8.27 vs 6.42).

**First test only.** After price has visited the level once, later tags
do not count — second visit, third visit, still sitting on it weeks later.
This is the same on **every** chart: US stocks, crypto, ETFs. It is a
price-action rule, not a market-specific rule.

AM 22.65: first cluster at the line is the signal; the later return is
not. CGSM 26.49: the circled first test is the signal; current price still
near the line weeks later is not. FEPI 42.34: it gained, then the next
week tested back — that first test is the signal; sitting on the line
now is not. IBDT 25.38 and JNK 96.7: after the weekly was lost, the
circled first rally back into the line is the signal; later tests are
not.

If the scanner was not running on that first test, do **not** fire when
price comes back. The first test already happened.

The same 1% / first-test rule applies to shorts.

---

## When the weekly candle is actually closed

Do not judge a take on a week that is still printing.

- **US cash:** the weekly prints at **Friday 4:00 PM America/New_York**.
- **Crypto:** no exchange close. The weekly prints at **Monday 00:00 UTC**
  (Sunday 23:59 UTC). In Indonesia that is 7:00 AM WIB / 8:00 AM WITA /
  9:00 AM WIT.

A forming Monday crypto bar is not last week’s close. Use the last
**closed** week.

---

## How later teaching should land

1. Write the new rule in this file (or a new `docs/` price-action note
   that this file points to).
2. Add or update fixtures under `tests/fixtures/weekly_screener/`.
3. Change `scripts/weekly_screener.py` last.
4. Do not encode one ticker as a special case.
