# socialops

Personal social media, content, and outreach automation for macOS. Phase B, milestone M1 (foundation).

## Setup (macOS, Python 3.12)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Commands

```bash
pytest                                  # run tests (external APIs are mocked)
python -m socialops check-config        # validate config/brands.toml
python -m socialops migrate             # apply migrations to the local DB
python -m socialops migrate --down-to 0 # roll back (dev only)
python -m socialops --dry-run status    # dry-run is the default
```

## Layout

- `config/brands.toml`: non-secret brand, cadence, and footer config. Same file on both Macs.
- Local DB: `~/Library/Application Support/SocialOps/app.db` (one per Mac, no sync).
- Logs: `~/Library/Logs/SocialOps/socialops.log` (token values redacted).
- Secrets: macOS Keychain, service names `socialops.<brand>.<platform>`. Never in this repo.
- Assets: `~/Library/Application Support/SocialOps/assets/<brand>/` (git-ignored).

## Rules

- Dry-run is on by default. Live writes need `--live`, and no live write path is enabled until M10.
- Excluded brands (BluePal Fox, "Compliance Without Panic") are rejected by config validation.
- Cold email is Microsoft 365 only, from `fred@clasbyfred.com`. Opt-outs and the footer are required.

## Caption drafting (M2B)

```bash
python -m socialops edit-post --id 1 --subject "HIPAA risk assessment basics" --visual-source canva
python -m socialops draft-week --brand clas_by_fred --week 2026-11-02          # dry-run: no model call
python -m socialops --live draft-week --brand clas_by_fred --week 2026-11-02   # calls Claude; costs money
```

- Live drafting reads the key from Keychain: `security add-generic-password -s socialops.claude -a api_key -w`. Do not paste the key into a command you keep in history.
- Drafts never change status. Approval is still required (`approve-week`).
- Model: `[llm] model` in `config/brands.toml`. Per-run cap: `max_drafts_per_run`.

## Connect Canva (per Mac, per person)

1. Create a Canva integration in your Canva developer account. Set the redirect URI to `http://127.0.0.1:8765/callback`.
2. Put its client ID in `config/brands.toml` under `[canva] client_id`. The client ID is not a secret.
3. If the integration has a client secret, store it. The `-w` flag with no value makes `security` prompt, so the secret is not typed into the command:
   ```bash
   security add-generic-password -s socialops.canva -a client_secret -w
   ```
4. Connect:
   ```bash
   python -m socialops auth canva          # opens the browser; sign in with your own Canva account
   python -m socialops auth canva-status   # shows connected / expiry; prints no tokens
   python -m socialops auth canva-disconnect
   ```

Tokens are stored in this Mac's Keychain under `socialops.canva`. Each person connects on their own Mac with their own Canva login.

## Publish to Meta (M3)

Setup, per Mac, run by the person who owns the Page/IG account (Pamela):

1. Put the Page ID and Instagram business account ID in `config/brands.toml` under each `account_id`. These are IDs, not secrets.
2. Store the Page access token. Use `-w` with no value so `security` prompts and the token is not typed into the command:
   ```bash
   security add-generic-password -s socialops.clas_by_fred.facebook -a access_token -w
   security add-generic-password -s socialops.clas_by_fred.instagram -a access_token -w
   ```
   Optional expiry (Unix seconds), so the app can warn 7 days before it lapses:
   ```bash
   security add-generic-password -s socialops.clas_by_fred.facebook -a expires_at -w
   ```
3. Set `media.public_base_url` to the public https base where published images and videos are served. Meta fetches media from that URL. Not set = nothing publishes.
4. Check: `python -m socialops token-status --brand clas_by_fred --platform facebook`

Flow: `approve-week` (human) -> `schedule-week` -> `publish-due` (dry-run by default; add `--live` to publish).

Safety: each publish is logged before the network call. Timeouts and 5xx on a POST are marked `unknown` and block retries, because the post may already be live. Check Meta before clearing those rows.

## Agents and QA (M4)

```bash
python -m socialops qa-week --brand clas_by_fred --week 2026-11-02   # runs the pre-post checklist on drafts
python -m socialops run-agents --brand clas_by_fred --week 2026-11-02 # QA, then publish due posts (dry-run by default)
```

- QA results are stored per content fingerprint. Editing a post after QA requires QA to run again before approval.
- Each agent run is logged in `agent_runs`. A failed agent is logged and does not stop the others.
- The orchestrator stops at the first agent that is held waiting for a human.
- Link checks run only in live mode, as read-only requests.

## Weekly approval page (M4C)

```bash
python -m socialops qa-week --brand clas_by_fred --week 2026-11-02
python -m socialops review --brand clas_by_fred --week 2026-11-02 --actor pamela
```

Open the printed address in a browser on the same Mac. The page lists the week's posts with their QA results. Approve or reject each one, or approve all that passed QA. Approving does not publish. Publishing runs on schedule for approved posts only. Press Ctrl+C in Terminal to stop the page.

- The page listens on 127.0.0.1 only and needs the per-run token in its address.
- Rejections need a reason and keep the post as a draft.

## Scheduler (M4D)

```bash
python -m socialops run-due                       # one tick, dry-run by default
python -m socialops --live run-due                # one tick, real publishing
python -m socialops --live install-schedule       # write the launchd job (every 5 minutes)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.socialops.run-due.plist
launchctl bootout gui/$(id -u)/com.socialops.run-due   # stop it
```

- Each tick: schedule approved posts whose slot is near, then publish what is due.
- Approved posts more than 6 hours past their slot are not scheduled automatically. Reschedule or re-approve them.
- Runs never overlap (file lock). Logs: `~/Library/Logs/SocialOps/run-due.*.log`.
- Install writes the job file only. Nothing runs until you load it with `launchctl`.

## Engagement and replies (M5)

```bash
python -m socialops answer-add --topic pricing --keywords "price,cost" --answer "..." --actor pamela   # approved answer bank
python -m socialops --live engagement-pull --brand clas_by_fred     # read new comments on recent posts (read-only)
python -m socialops reply-draft --brand clas_by_fred                # answer bank first, then Claude
python -m socialops reply-list --brand clas_by_fred                 # review what's there
python -m socialops reply-approve --id 12 --actor pamela            # approves the exact text shown
python -m socialops --live reply-send --brand clas_by_fred          # sends approved replies only
```

- Comments are read, never liked, hidden, or followed. Legal, complaint, and refund topics are escalated and get no draft. Spam gets no draft.
- Editing a reply after approval means approving it again before it can send.
- A failed send is marked `failed` and not retried automatically. Daily cap: 20 per brand.
- Direct messages are not built yet. They need Meta messaging permission, which needs app review.

## Webinars and YouTube replays (M6)

Setup (once, on Pamela's Mac or yours):
1. Google Cloud: create a project, enable YouTube Data API v3, create an OAuth client (Desktop app).
2. Put the client ID in `brands.toml` under `[youtube] client_id`. Store the client secret and tokens in Keychain under `socialops.youtube`.
3. The YouTube channel must be verified. Unverified API projects upload private only.

```bash
python -m socialops webinar-add --brand clas_by_fred --title "HIPAA risk basics" --starts 2026-12-02T19:00 \
  --registration-url https://classbyfred.com/webinar --calendly-url https://calendly.com/fred/intro
python -m socialops webinar-promote --id 1              # promo posts take the 3 weeks of webinar slots
python -m socialops webinar-status --id 1 --status live
python -m socialops webinar-replay-file --id 1 --file assets/clas_by_fred/replay.mp4 --chapters "00:00 Welcome|05:12 Risk|12:40 Next"
python -m socialops --live replay-upload --id 1         # uploads PRIVATE, within 48 hours
python -m socialops webinar-list --brand clas_by_fred   # shows OVERDUE if the deadline passes
python -m socialops replay-approve-public --id 1 --actor pamela
python -m socialops --live replay-publish-public --id 1
python -m socialops youtube-quota
```

- Registration pages must be on the brand website (host the MailerLite form there). Calendly links must be calendly.com.
- Quota: uploads cost 1,600 units, visibility changes 50. The app stops at 90% of the daily 10,000.
- Going public needs your approval, recorded separately from the upload.
- Replays are never uploaded automatically. Run `replay-upload` yourself.

## Visuals (M7)

- Canva rendering needs: the brand's `canva_template_id` in `brands.toml`, the field mapping (`canva_fields`), and `[canva] api_verified = true` after the endpoint paths in `src/socialops/visual.py` are checked against Canva's developer docs. Until then rendering refuses to run.
- Your own Canva login: `python -m socialops auth canva` on your Mac (see the Canva section above).
- Render: `python -m socialops --live visual-render --id 12` saves the PNG under `assets/<brand>/exports/` and runs the asset check.
- Asset check on any image: `python -m socialops visual-check --brand clas_by_fred --week 2026-11-02`. Checks: 600px minimum side, 4:5 to 1.91:1 aspect, 10 KB to 8 MB, real PNG or JPEG.
- A failed asset check blocks approval for that post until the file changes and the check passes. Checks are not yet required for every post; run `visual-check` before approving a week.
