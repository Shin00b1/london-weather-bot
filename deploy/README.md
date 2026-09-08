# DigitalOcean deployment — London weather bots

Target: one small droplet running dawn_watcher (min-side taker), field_maker
(max-side paper MM) and book_recorder 24/7. This kills the Mac-sleep problem:
the 03:00-07:30Z dawn window is finally always covered.

## 1. Create the droplet (console, ~5 min)

- Image: Ubuntu 24.04 LTS
- Plan: Basic Regular — 2 GB / 1 vCPU / 50 GB ($12/mo). 1 GB ($6/mo) works too;
  upgrade later only if you see OOM.
- Region: NYC1 or NYC3 (Polymarket CLOB + aviationweather + Iowa API are all
  US-hosted — east coast is the low-latency choice; London adds nothing).
- Auth: SSH key (generate locally if needed: `ssh-keygen -t ed25519`)
- extras: enable weekly backups (+20%) if you want; otherwise snapshots manually.

## 2. Push the code (from the Mac)

```bash
ssh-keygen -t ed25519            # once, if no key exists
# add the .pub to DO when creating the droplet, or via console afterwards

rsync -av --rsync-path="mkdir -p /opt/london/deploy && rsync" \
  --include='london/***' --include='deploy/***' \
  --include='dawn_watcher.py' --include='field_maker.py' \
  --include='book_recorder.py' --include='tape.py' \
  --include='falcon_client.py' --include='.env' \
  --exclude='*' \
  ./ root@DROPLET_IP:/opt/london/
```

This copies the state dir (tape.duckdb ~240 MB, jsonl logs, .env) plus just the
five scripts the bots need. .env holds FALCON_API_TOKEN — it is chmod 600 by
bootstrap; never commit or paste it.

## 3. Bootstrap (on the droplet)

```bash
ssh root@DROPLET_IP
cd /opt/london
bash deploy/bootstrap.sh
```

Then verify:

```bash
systemctl status london-fieldmaker london-dawnwatcher london-bookrecorder.timer
journalctl -u london-fieldmaker -f        # live logs
tail -f /opt/london/london/field_maker.log
```

Server is UTC by default — all bot windows (03-07:30Z, 06-20Z) line up as-is.

## 4. What moves vs what stays

MOVES to the droplet: dawn_watcher, field_maker, book_recorder (and later the
Falcon backfills). Once the server is verified for a few days, disable the Mac
copies so paper fills aren't duplicated:

```bash
launchctl unload ~/Library/LaunchAgents/com.london.dawnwatcher.plist
launchctl unload ~/Library/LaunchAgents/com.london.fieldmaker   # (n/a if absent)
sudo launchctl bootout system/com.london.bookrecorder 2>/dev/null || \
  launchctl unload ~/Library/LaunchAgents/com.london.bookrecorder.plist
```

The pmset 04:45 wake + caffeinate machinery becomes unnecessary for the bots.

STAYS on the Mac: the ZCode automations (sheet updates, morning 10:30, night
21:45), the xlsx workbooks, research/backtests.

## 5. Ongoing

- Fetch results back for research when you want:
  `rsync -av root@DROPLET_IP:/opt/london/london/*.jsonl ./london/server_logs/`
- Optional free heartbeat: create a monitor on healthchecks.io and add
  `curl -fsS https://hc-ping.com/<uuid> ` to each wrapper line — emails you if
  a bot stops heartbeating.
- Going LIVE later: order-signing needs a wallet key on the server. Use a
  fresh dedicated wallet, key in .env chmod 600, fund it small. Never reuse
  your personal wallet key.
