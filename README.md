# Main Line Trades — YouTube replay to Discord

This workflow checks the public YouTube feed every 10 minutes, filters titles beginning with:

`🔴 Live trading Crypto Futures Forex Stocks - NY Open`

and posts new matches to Discord.

Add these repository secrets:

- `YOUTUBE_CHANNEL_ID`
- `DISCORD_WEBHOOK_URL`

First manual run with **Send a test message to Discord** enabled tests the webhook.
First normal run records existing matching videos without posting old replays.
