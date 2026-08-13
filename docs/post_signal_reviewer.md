# Post-Signal Reviewer

## Purpose

Review Main Line Trades signals after enough weekly price action has developed,
while keeping staff in control of every public result.

This workflow applies only to signals published after the reviewer foundation is
deployed. It does not backfill older Signals posts.

## Approved workflow

1. A Manual Signal Composer or Earnings Review signal is successfully published
   to the public Signals channel.
2. The same state transaction that confirms delivery creates one scheduled
   post-signal review record.
3. The first review becomes due one calendar month after the signal timestamp.
4. A future daily review job retrieves the original Signals message and its
   original chart attachment.
5. The system generates a current weekly chart using the same visual style as
   Earnings Movers.
6. The new chart preserves:
   - the original chart's visible time horizon;
   - exactly one mandatory horizontal reference level;
   - the candle/date where that single level begins;
   - all newly completed candles after the original signal.
7. The review calculates performance from the single reference level to the
   current price. Long performance follows the price change; Short performance
   inverts it so a decline from the reference level is shown as a gain.
8. A private review card shows the original chart, updated chart, direction,
   reference level, current price, direction-adjusted gain/loss, thesis,
   elapsed time, and an outcome summary.
9. Staff chooses one of three actions:
   - **Publish**: send the approved card to the public signal-results channel.
   - **Pend One Month**: schedule the same signal for another calendar-month
     review without publishing a public result.
   - **Dismiss**: close the review without publishing it.

No review is published automatically in the first version.

## Foundation state

Each newly confirmed Signals message creates one record keyed by its unique
Discord message ID. The record retains:

- source workflow (`earnings` or `manual`);
- source record ID;
- Signals channel and message IDs;
- symbol or instrument;
- mandatory Long/Short direction;
- one mandatory chart reference level;
- original trade thesis;
- original chart filename;
- sent timestamp;
- next review due timestamp;
- review cycle and status.

The Signals message remains the canonical source for the original chart file.
The state record stores its Discord provenance instead of duplicating chart
bytes in runtime JSON.

## Safety rules

- Review scheduling occurs only after Discord returns a valid Signals message
  ID.
- Delivery confirmation and review scheduling are persisted together.
- A malformed review record fails closed and cannot enable another Signals
  delivery.
- The Signals message ID provides idempotency: one published signal can have
  only one active review record.
- Missing original messages or attachments require staff reconciliation.
- Publishing and pending will use transactional reservations so concurrent
  button interactions cannot produce duplicate results.
- The bot must never infer Long or Short from chart appearance; it uses the
  mandatory direction saved with the original signal.
- The bot must never choose among multiple chart lines. New signals permit one
  official reference line only, and that saved price is the sole performance
  baseline used by the reviewer.

## Later implementation phases

1. Private and public Discord channel configuration.
2. Due-review daily timer and private draft creation.
3. Single-reference-level preservation and updated weekly chart generation.
4. Staff Publish / Pend One Month controls.
5. Public results feed and aggregate performance reporting.
