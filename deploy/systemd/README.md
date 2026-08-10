# Main Line Trades systemd units

These files mirror the production units installed in `/etc/systemd/system/` on
the Main Line Trades Linux host.

Install or update them from the repository root:

```bash
sudo cp deploy/systemd/mainline-* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mainline-earnings-review.service
sudo systemctl enable --now \
  mainline-earnings-post.timer \
  mainline-market-wrap.timer \
  mainline-morning-brief.timer \
  mainline-trendspider.timer \
  mainline-trump-filter.timer \
  mainline-weekly-calendar.timer \
  mainline-youtube-replays.timer
```

Verify the deployment:

```bash
systemctl list-timers --all | grep mainline
systemctl --failed | grep mainline || true
systemctl status mainline-earnings-review.service --no-pager
```

The units assume the repository is at
`/home/jason/main-line-trades-replays` with its virtual environment at
`.venv/`. Secrets remain in the project's untracked `.env` file and must not be
added to these unit files.
