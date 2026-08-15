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
  mainline-weekly-calendar.timer \
  mainline-youtube-training.timer \
  mainline-youtube-video-intel.timer \
  mainline-youtube-replays.timer
```

Verify the deployment:

```bash
systemctl list-timers --all | grep mainline
systemctl --failed | grep mainline || true
systemctl status mainline-earnings-review.service --no-pager
```

Retired timers (disable on production if still enabled):

```bash
sudo systemctl disable --now mainline-trendspider.timer
sudo systemctl disable --now mainline-trump-filter.timer
sudo rm -f /etc/systemd/system/mainline-trendspider.service \
             /etc/systemd/system/mainline-trendspider.timer \
             /etc/systemd/system/mainline-trump-filter.service \
             /etc/systemd/system/mainline-trump-filter.timer
sudo systemctl daemon-reload
```

The units assume the repository is at
`/home/jason/main-line-trades-replays` with its virtual environment at
`.venv/`. Secrets remain in the project's untracked `.env` file and must not be
added to these unit files.

Post-signal review chart OCR also requires the system package:

```bash
sudo apt-get install -y tesseract-ocr
```
