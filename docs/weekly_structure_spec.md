# Weekly Gain / Loss — Price Action Teaching

This file is the locked teaching for how Jason finds a **weekly gain** or
**weekly loss**. Later price-action lessons should be added here (or as
sibling docs under `docs/`) the same way: write the rule in English first,
then change fixtures, then change code.

The classifier in `scripts/weekly_screener.py` must follow these rules only.
Do not special-case live tickers. If a new chart disagrees with a rule
below, change this file first.

Trained from Jason’s charts: LNSR 6.42, MH 12.21, CRMD 7.13, PODC,
POWERGRID, BSX, SGA, and NFLX 77.65.

---

## What this is not

It is **not** last week’s high or low.

Comparing this week’s close to the prior week’s wick high/low is the wrong
method. That is a different pattern and will fire on noise (tiny crypto
breaks, etc.).

The weekly is a **containment** from a swing extreme back to the origin of
the move that made that extreme.

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
4. Intermediate candles on the way down (the MH **X** near 10.50) are
   **not** the weekly. They did not make the low.
5. **Re-anchor rule:** if price **does** continue and print a new low,
   the weekly moves to the origin of **that** later drop. Hypothetical:
   if MH had kept falling from the X area into a new low (~8.30), the
   weekly would have moved to that later candle’s **body** (~10.50). It
   did not, so **12.21 stays**.
6. The weekly line is the **origin of the drop that made that low** —
   the top of the containment (top swing bodies ↔ the low).
7. Draw the line on that origin candle’s **body**:
   `max(open, close)`. **Never the wick.**
8. **Gained the weekly** = a later weekly candle **closes through** that
   body line (close strictly above it).

### Trained gain examples

| Name | Origin body (the weekly) | Notes |
| --- | --- | --- |
| LNSR | 6.42 | New low ~5. This week through 6.42 (close 8.27) is a take, still far from the line. |
| MH | 12.21 | Low is the small-circle low. X is not the weekly. |
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
3. If it **does** make a new high, re-anchor to the origin of **that**
   rally.
4. Draw on that origin candle’s **body**: `min(open, close)`. Never the
   wick.
5. **Lost the weekly** = a later weekly candle **closes through** that
   body line to the downside (close strictly below it).

Most names that qualify will be gained / Long. Sometimes it is lost /
Short. Both are valid.

---

## Taken vs still on the line

Gaining or losing the weekly is the **close through** the origin body.

After that, two situations both matter:

- Price has run **away** from the body (LNSR 8.27 vs 6.42, PODC still
  above 2.43). The weekly has been taken. That is watch-only until price
  comes back.
- Price is **within 1%** of that same body:
  `abs(last - level) / level <= 0.01`

  That 1% includes a later retest into the line **or** the **same week
  it was taken**, if the close is still inside 1% (NFLX 78.16 vs 77.65).
  Do **not** wait for a deeper pullback in that case.

The same 1% rule applies to shorts.

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
