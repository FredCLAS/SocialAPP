# Game Plan: Personal Social, Content & Outreach App (macOS)

Status: Phase A draft. No application code until Phase B approval.
Source spec: operator brief (brands, cadence, strategy modes, lead pipeline, Phase A/B rules).
Knowledge base (/kb/) step: BLOCKED. /kb/ is not present in the current environment. See section 7.

---

## 0. Scope and hard rules

- Brands: `clas_by_fred` (Facebook, Instagram, LinkedIn, YouTube replays), `pamela_art` (Facebook, Instagram).
- Excluded: BluePal Fox, "Compliance Without Panic". No profiles created.
- Machines: Pamela (MacBook Pro) is the primary operator. She posts for both brands, runs the lead engine, approves all posts, and performs all authorizations (Meta, Google, Canva, Microsoft, Zoice) under her own accounts. Frederic (MacBook Air) is backup and handles specific tasks only. Same codebase, one local SQLite DB per Mac, no sync. Frederic's Mac does not post unless coordinated with Pamela.
- Rules that override features:
  - Never open, read, parse, copy, or commit the shared logins file.
  - All tokens in macOS Keychain. No secrets in code, config, or repo. `.env` files git-ignored.
  - Canva: OAuth only. No passwords.
  - LinkedIn: no automated connection requests, DMs, or scraping.
  - MailerLite: opted-in subscribers only. Cold email from personal mailbox only.
  - Prohibited: mass friend requests, auto-likes/comments, pods, mutual engagement with own accounts, follow-gated giveaways, purchased followers, scraping sites that forbid it.

---

## 1. Funnel

```
Content (FB / IG / LI / YT)
   -> CTA link
      -> MailerLite opt-in (webinar registration, lead magnet)  --> Google Sheets CRM (stage: found/contacted/...)
      -> Calendly (1:1 booking)                                  --> Google Sheets CRM (stage: booked)

Cold path (clas_by_fred only):
lead_engine (public registries + company sites)
   -> verified email, source recorded
   -> qualified (human review)
   -> cold sequence from personal mailbox (day 0 / 4 / 10; first send approved)
   -> reply -> sequence stops -> Calendly link -> CRM (booked)
```

Webinar loop: promote T-21 days (posts, MailerLite opt-in, Calendly) -> webinar -> replay to YouTube within 48h with chapters and Calendly link -> replay link in follow-up posts and MailerLite sequence.

---

## 2. Weekly calendar (default; configurable per brand/platform in `brands.toml`)

| Brand | Platform | Per week | Mix | Days |
|---|---|---|---|---|
| clas_by_fred | Facebook | 7 | 4 image+checklist, 2 questions, 1 webinar/booking CTA; 3 of 7 are video | 1 per day |
| clas_by_fred | Instagram | 7 | Same mix; Zoice videos published as Reels | 1 per day |
| clas_by_fred | LinkedIn | 3 | Text + image, aimed at owners in regulated sectors. Manual post | Tue, Wed, Thu |
| clas_by_fred | YouTube | 1 per webinar | Replay with chapters + Calendly link | Upload within 48h of webinar |
| pamela_art | Facebook | 4 | Art; AI-labeled or illustrative, or real photos of actual pieces | Mon, Wed, Fri, Sun |
| pamela_art | Instagram | 4 | Same rules as Facebook | Mon, Wed, Fri, Sun |

Monthly: 1 webinar. Promotion starts T-21 days.
Weekly: Sunday weekly report (AUTO).
Default posting time: 08:30 Eastern (America/New_York); LinkedIn 10:00 Eastern. Both configurable.

Engagement: report only. The app shows counts and trends. It never likes, comments, follows, or joins pods.

---

## 3. Automation map

| Task | Mode | Notes |
|---|---|---|
| Content calendar generation | AUTO | Creates post records from cadence config |
| Caption, script, hashtag drafting | AUTO (draft) | Claude API. Drafts are inert until approved |
| Post approval | Pamela | Weekly batch review; nothing schedules without it |
| Scheduling and cross-posting FB/IG | AUTO | Only after post status = approved |
| LinkedIn posts | MANUAL | App produces copy + image; user posts |
| Insights pull (FB/IG) | AUTO | Read-only |
| Weekly report | AUTO | Local, stored in DB |
| Webinar promotion (post scheduling) | AUTO | Uses approved promo posts |
| MailerLite opt-in handling | AUTO | Opted-in subscribers only |
| YouTube replay upload | AUTO, private | Public visibility flip = ask first |
| CRM logging to Google Sheets | AUTO | Stage changes sync |
| Facebook Live reminders | AUTO | Notification only |
| Comment and DM reply drafts | APPROVE | Never sent without approval |
| Cold email first send per contact | APPROVE | Per-contact approval record required |
| Cold email follow-ups (day 4, 10) | AUTO after approval | Only to contacts already approved; stops on reply |
| Boost recommendations | APPROVE, currently OFF | Milestone ladder: recommend at >300, >1,000, >10,000 views. Each boost needs Pamela's approval. Never spends automatically |
| Zoice video production | MANUAL queue | Upload by hand until Zoice API confirmed |
| Reaction Follow Invites, Favorite Follow, Influencer Inner Circle | MANUAL | Tracked in app as checklist only |
| Engagement (likes, comments, pods) | PROHIBITED | Report only |
| LinkedIn connection requests, DMs, scraping | PROHIBITED | |

---

## 4. Architecture

### 4.1 Stack
- Python 3.12, SQLite (stdlib `sqlite3`), pytest, mocked external APIs in tests.
- Packaging: `pyproject.toml`, `src/` layout. Keychain via `security` CLI wrapper (no third-party secret store).
- Global `--dry-run` flag and `DRY_RUN` per brand in config. Dry-run = no outbound HTTP for writes, logs the would-be request.

### 4.2 Modules

| Module | Responsibility |
|---|---|
| `content_engine` | Generates post records from cadence; calls Claude API for captions/scripts |
| `post_queue` | Post state machine (draft, approved, scheduled, published, failed); approval queue |
| `publisher` | `meta` (FB Pages, IG Graph API), `youtube` (resumable upload, chapters) |
| `visual_engine` | Canva Connect: fill brand template fields, export PNG |
| `video_queue` | Zoice manual queue: script, due date, asset path, upload-done marker |
| `lead_engine` | Import NE Florida list, registry ingest, email extraction, MX verification |
| `outreach_sender` | Approval-gated cold email; cap 30/day; sequence; footer check |
| `crm_sync` | Google Sheets push/pull for leads and bookings |
| `webinar_promo` | T-21 promo schedule, MailerLite opt-in links, Calendly link injection |
| `weekly_report` | Insights aggregation, cadence adherence, CRM funnel counts |
| `scheduler` | Runs due jobs; see 4.3 |
| `secrets` | Keychain read/write by `service = socialops.<brand>.<platform>` |
| `config` | `brands.toml` (no secrets), validates cadence and brand list |

### 4.3 Scheduler
- Recommended: `launchd` user agent running `python -m app run-due` every 5 minutes. Survives reboot. No always-on process needed.
- Each job is idempotent, keyed on `(post_id, platform)` or `(lead_id, step)`.

### 4.4 Approval queue
- `approvals` table: `id, kind (post|reply|cold_send|boost), ref_id, decided_by, decided_at, decision`.
- Nothing with kind in {reply, cold_send, boost} executes without an `approved` row.
- Posts: status must be `approved` before the publisher reads them.

### 4.5 Data (per Mac, SQLite)
- Location: `~/Library/Application Support/SocialOps/app.db`.
- Tables: `brands`, `platform_accounts` (ids only, no tokens), `posts`, `assets`, `approvals`, `publish_log`, `videos_queue`, `webinars`, `leads`, `lead_sources`, `contacts`, `outreach_steps`, `opt_outs`, `insights`, `reports`, `quota_ledger` (YouTube units), `schema_migrations`.
- Post record: `{brand, platform, subject, visual_path, caption, hashtags[], cta_url, scheduled_at, status}`.
- Status flow: `draft -> approved -> scheduled -> published`; any -> `failed` (with retry count). Invalid transitions rejected in code and tested.

### 4.6 Assets
- Per brand, per Mac: `~/Library/Application Support/SocialOps/assets/<brand>/`.
- Post records store relative paths. Files are never uploaded to the repo.
- Canva exports land in `.../assets/<brand>/exports/`.

### 4.7 Keychain
- Items: Meta user/page tokens, IG ids, Google OAuth refresh token (YouTube, Sheets), Microsoft Graph refresh token (Outlook send), Canva access/refresh tokens, Zoice key (when issued), Claude API key, MailerLite key, Calendly token.
- Keychain lives on the Mac where the authorization was done. Pamela's Mac holds Pamela's tokens.
- Access only through `secrets` module. Tests use an in-memory fake.

---

## 5. Platform setup

### 5.1 Meta (Facebook Pages + Instagram)
1. Meta Business Center (Business Manager): already exists. Pamela is the connected admin. Confirm each brand's Facebook Page and Instagram account is linked to the BM under Pamela. Frederic gets access only if needed as backup, with his own Meta account.
2. Instagram: switch each account to Business or Creator and link to its Facebook Page.
3. developers.facebook.com: create app (type Business). Add products: Facebook Login for Business and Instagram Graph API. Keep app in **Development mode**.
4. App Roles: add Frederic and Pamela as Administrator or Developer. Development mode serves only app roles.
5. Permissions to request (dev mode, own Pages and accounts): `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`, `business_management`. Verify exact names against current docs.
6. OAuth: each user signs in from the app. Exchange for long-lived user token, derive Page tokens, store in Keychain.
7. Instagram publishing uses a container flow (create container, then publish). Limit 100 API-published posts per 24h per account.
8. Advanced Access and Business Verification are needed only if non-role users must authorize. Not required for this setup. Re-check before Phase B.
9. Token expiry: long-lived user tokens ~60 days. App shows expiry and warns 7 days before.

### 5.2 Google Cloud and YouTube
1. Create project `socialops-personal`.
2. Enable **YouTube Data API v3** and **Google Sheets API**.
3. OAuth consent screen: External. Add Frederic's and the channel owner's accounts as test users. Scopes: `youtube.upload`, `youtube.force-ssl` (chapters in description), `spreadsheets`.
4. OAuth client type: Desktop app. Store client ID and tokens in Keychain. Loopback redirect.
5. Channel must be verified (required for uploads over 15 minutes).

**YouTube quota math**
- Default quota: 10,000 units per project per day.
- `videos.insert` = 1,600 units. `videos.update` (chapters) = 50 units.
- One replay = 1,650 units. Daily ceiling ≈ 6 replays.
- Expected load: 1 replay per month, plus retries. Usage ≈ 1,650 per month. Headroom is more than 99%.
- Unverified API projects: uploads are restricted to private until the API audit is passed. Plan: upload private, flip to public after Frederic approves. Audit request only if needed.
- `quota_ledger` table records every call's unit cost; the app stops at 90% of daily quota.

### 5.3 Canva Connect API
1. Canva developer portal: create integration. Use OAuth 2.0 with PKCE and a loopback redirect.
2. Scopes (verify names): design read/write, brand template read, asset upload, folder read, export.
3. Pamela authorizes with her own Canva profile on her MacBook Pro. Tokens go in Pamela's Keychain. Refresh tokens rotate; store each new one immediately.
4. Canva Connect API access confirmed by operator. Still verify that autofill and PNG export are available on Pamela's plan before M5.
5. `visual_engine` runs on Pamela's Mac only. Frederic does not hold Canva tokens.
6. Canva plan includes PNG export (operator confirmed).
6. Brand template for `clas_by_fred`: logo, checklist layout, headline field, subject field. Template ID in `brands.toml`.

### 5.4 Cold-email mailbox
- Provider: Microsoft 365 mailbox on the company domain (owned). Send via Microsoft Graph `POST /me/sendMail` with delegated `Mail.Send` scope, after approval. Auth: MSAL public-client OAuth (device or loopback), refresh token in Keychain. Authorized by Pamela on her own Microsoft account.
- Domain setup: SPF, DKIM, DMARC on the sending domain. Verify with a test send to a seed inbox before any real send.
- Warm-up: 2 to 3 weeks at low volume before reaching the 30/day cap.
- Cap: 30 sends per day, enforced in code and by a DB count. Well under Microsoft 365 daily limits.
- Each email body must contain: sender name, company, physical postal address, unsubscribe line (`reply STOP`). Missing any element = send refused.
- Footer address: 6271 St Augustine Rd Ste 24, Jacksonville, FL 32217, US.
- Sender mailbox: one per brand/sender, set in `brands.toml`. Pending operator choice for CLAS cold email (see open questions Q10).
- Opt-outs: any reply or STOP stops the sequence and adds the address to `opt_outs`. Checked before each send.
- MailerLite is not used for cold email (terms prohibit it).

### 5.5 LinkedIn
- No API use. Community Management API for company pages needs LinkedIn partner approval. Assume unavailable. Revisit only if approved.
- App produces copy and image. The user posts manually.
- No connection requests, no DMs, no scraping, no Sales Navigator automation.

### 5.6 MailerLite, Calendly, Google Sheets
- MailerLite: Light plan. API access and opt-in forms confirmed by operator. API key in Keychain. Opt-in forms for webinar registration and lead magnet.
- Calendly: personal access token. Read event-type URLs for CTA injection. Optional webhook `invitee.created` for CRM booked stage.
- Google Sheets: CRM spreadsheet with tabs `Leads`, `Contacts`, `Bookings`. Columns include `stage`, `source_url`, `email_source`, `last_touch`. Sync is one-way push from DB with a pull for manual stage edits (Phase B decision).

---

## 6. Lead pipeline

- ICP: US companies, 10+ employees, regulated sectors (healthcare, real estate, legal, CPA). Priority Northeast Florida. Fallback nationwide.
- Import: existing NE Florida target list first. Accept CSV and XLSX. Path to be provided by operator.
- Sources (free, public): state licensing boards, Florida Sunbiz, CMS NPPES, company websites, Google Places within free tier.
- Scraping rule: fetch only pages that the site's robots.txt and terms allow. Rate-limit per domain. Skip any site that prohibits it.
- Email: taken from company websites only. Verify syntax, then MX lookup, then a free verifier. Store `email_source` (URL) with each address.
- Stages: `found -> qualified -> contacted -> replied -> booked -> won | lost`.
- Targets: 30 new prospects per week. 2 booked calls per week by week 8. Review at week 4 using reply rate.
- Cold sequence: day 0, day 4, day 10. Stops on any reply or opt-out. First send to each contact requires approval.
- Each email: sender identity, physical address, unsubscribe line, Calendly link.
- LinkedIn and phone: drafts and call scripts only. User sends.

---

## 7. Knowledge base (Phase A step 1)

- Required: `pdftotext -layout` on every PDF in `/kb/`. Flag any with no extractable text.
- Location: `~/Desktop/CLASbyFRED/Tools/KB/` on the operator's Mac. Extraction run 2026-11-03 with poppler 26.10.0 on the Mac.
- Result: 41 PDFs in scope, all with extractable text (character counts 788 to 104,615). No scanned-only files. Four course packages: Facebook, Instagram, YouTube, Sales Funnel. Non-KB ebook `ebook-understanding-the-agentic-enterprise-era.pdf` removed by operator; must not be in the KB folder or used.
- Harmless warnings: "Invalid Font Weight" on three 2021 flowcharts. Text still extracted.
- Plan once available: extract to `docs/kb-extract/` (git-ignored). Use only to paraphrase principles. Never publish or reproduce KB text. Four packages expected: Facebook, Instagram, YouTube, Sales Funnel.

---

## 8. Roadmap and acceptance criteria

| Milestone | Scope | Acceptance criteria |
|---|---|---|
| M0 Phase A | This plan + open questions | Approved by Frederic |
| M1 Foundation | Repo skeleton, config, SQLite schema + migrations, Keychain wrapper, logging, `--dry-run` | pytest green. Migrations up/down. Keychain round-trip passes with fake store. Dry-run test asserts zero outbound HTTP calls |
| M2 Content + queue | Cadence generator, post state machine, approval queue | Weekly generation matches cadence counts for every brand/platform. Invalid status transitions raise. Re-running generation is idempotent |
| M3 Meta publisher | FB Page and IG container publishing (mocked Graph API) | Publish path tested for success, retry, and failure. Retries never double-post (keyed by post_id). Token expiry warning fires at T-7 days |
| M4 YouTube + webinar promo | Resumable upload, private-then-public gate, chapters, T-21 promo schedule, 48h deadline alert | Quota ledger records 1,650 units per replay. Public flip requires approval record. Deadline alert fires at 36h if not uploaded |
| M5 Visual engine | Canva Connect fill + export (mocked) | Fields filled from post record. PNG saved to asset path. Token refresh rotates and stores new refresh token |
| M6 Video queue | Zoice manual queue | Queue lists scripts with due dates. Mark-uploaded updates post status. No API call |
| M7 Insights + report | Pull insights, weekly report | Report reproduces from stored insights. No like/comment/follow code paths exist (grep test) |
| M8 Leads + CRM | NE Florida import, dedupe, MX check, verifier (mocked), Sheets sync (mocked) | Every email has `email_source`. Duplicate domains merged. Sheets sync idempotent |
| M9 Outreach sender | Approval-gated sends, cap, sequence, footer check, opt-outs | Send without approval record raises. 31st send in a day refused. Day 4 and day 10 scheduled correctly. Reply stops sequence. Missing footer element refuses send |
| M10 Live pass | Each real API in dry-run, then one approved real action each | Each live step needs explicit user approval (publish, spend, send). Logged in `publish_log` |

Coverage target: 85% on `post_queue`, `outreach_sender`, `publisher`.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Meta dev-mode limits or token expiry | Expiry warnings; re-auth flow in app |
| Canva plan does not include Connect features | Manual template fill fallback; check plan first (Q2) |
| Cold-email deliverability or spam complaints | Warm-up, cap 30/day, opt-outs, footer checks, seed tests |
| Scraping terms | robots.txt and terms check per domain; skip prohibited sites |
| Two Macs, two DBs diverge | Pamela's Mac is the single posting and lead-engine host. Frederic's Mac is backup only and must not post without coordination. No cross-Mac sync |
| Accidental public publish | Public actions require approval record; dry-run default in config until M10 |

---

## 10. Phase B order (after approval)

1. M1 foundation. 2. M2 content and queue. 3. M3 Meta. 4. M4 YouTube and promo. 5. M5 Canva. 6. M6 Zoice queue. 7. M7 reports. 8. M8 leads and CRM. 9. M9 outreach. 10. M10 live pass with approvals.

Each step asks before anything that publishes publicly, spends money, or sends email.
