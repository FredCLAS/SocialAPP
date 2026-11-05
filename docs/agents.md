# Agent design: socialops

Purpose: an automation app where agents and sub-agents do the work and a human approves anything that goes public or talks to a person.
Source of workflow structure: the KB course packages (stages, checklists, gates). Marketing advice is excluded.

## 1. Rules every agent follows

1. **Gates.** An agent may prepare anything. It may not publish, send, reply, spend, or make a visibility change without an approval record (`approvals` table). Gate kinds: `post`, `reply`, `cold_send`, `boost`, `visibility`, `connect`.
2. **Dry-run first.** Every external write goes through `Guard`. Default is dry-run.
3. **No engagement automation.** No likes, comments, follows, pods, or mutual engagement. Read-only reporting only.
4. **No promised results.** QA rejects guarantees and unverifiable claims.
5. **One writer per resource.** Only the owning agent writes a table or external resource. Others read.
6. **Every run is logged.** `agent_runs` records agent, inputs (ids only), outcome, and reason. No secrets or message bodies in logs.
7. **Human-readable reasons.** Every rejection, block, and hold includes a reason a person can act on.

## 2. Workflow state machines (from the course stages)

**Post** (already in M2/M3): `draft → approved → scheduled → published | failed`. Only `approved` posts can schedule.

**Lead** (course: pipeline stages): `found → qualified → contacted → replied → booked → won | lost`.
- `found → qualified` needs a human decision (gate: qualification).
- `contacted → replied` happens only from the reply classifier. Any reply stops the outreach sequence.

**Outreach sequence** (course: follow-up cadence): day 0, day 4, day 10. Stops on reply or opt-out. First send to each contact needs approval; follow-ups run automatically only to contacts already approved.

**Webinar** (course: promote, host, replay): `planned → promoting (T-21) → live → replay_pending (≤48h) → replay_published`.

**Video** (course: create, optimize, publish): `script → queued_for_video → produced → optimized → approved → published`.

**Reply/Q&A item**: `received → classified → drafted → approved → sent | rejected | expired`.

## 3. Agents

Each agent lists: purpose, inputs, outputs, sub-agents, gate, and tool access. "Tool" means an internal module or an external API.

### 3.1 Orchestrator
- Purpose: runs the schedule, routes work to agents, holds the approval inbox, and shows status.
- Inputs: config, clock, approval decisions.
- Outputs: job runs, approval requests, daily digest.
- Sub-agents: none (it is the root).
- Gate: none directly. Enforces all gates.
- Tools: scheduler (launchd), `post_queue`, `approvals`, `agent_runs`.

### 3.2 Calendar agent (course: content planner, weekly cadence)
- Purpose: builds the week's slots from cadence and mix.
- Inputs: `brands.toml` cadence, week start.
- Outputs: draft post slots (M2, done).
- Sub-agents: none.
- Gate: none (slots are drafts).
- Tools: `content_engine`.

### 3.3 Content writer agent (course: swipe files → captions; post types)
- Purpose: drafts captions, hashtags, CTA, video scripts from a topic.
- Sub-agents:
  - Caption drafter (M2B, done).
  - Script writer (video scripts for Zoice; output `script` state).
  - Topic proposer (suggests topics from the calendar mix; human picks).
- Gate: draft only; the QA agent and the human approve.
- Tools: Claude API (`llm`), `post_queue`.

### 3.4 Visual agent (course: visual templates)
- Purpose: fills Canva brand templates from the post record and exports PNG.
- Sub-agents:
  - Template filler (Canva autofill, M5).
  - Asset checker: file type, size, aspect ratio, AI-label flag from `visual_source`.
- Gate: none (asset only). Asset checker failure blocks approval.
- Tools: Canva Connect (Pamela's token), local asset folder.

### 3.5 Video queue agent (course: video creation checklist)
- Purpose: tracks Zoice jobs and manual upload until the Zoice API is confirmed.
- Sub-agents: none.
- Gate: none. Visibility change is gated separately.
- Tools: `videos_queue` table; Zoice API when available.

### 3.6 QA agent (course: pre-post checklist → automated Q&A step)
- Purpose: runs every check from the post checklist before a human sees the post. Output: pass, or fail with reasons.
- Checks (rule-based first, LLM only where rules cannot decide):
  1. Visual present and matches `visual_source`.
  2. Caption present; hook, value, and CTA structure present.
  3. CTA URL on the allowed host list.
  4. Hashtags: count and format.
  5. Platform limits: caption length.
  6. AI visual labeled in caption.
  7. Banned claims and guarantees.
  8. Scheduled time is inside the brand's posting slot.
  9. Audience fit for the brand (LLM check against the brand's topic list).
- Sub-agents:
  - Claims checker (LLM, returns quoted phrase + reason for any flag).
  - Link checker (HTTP HEAD on CTA URL; read-only, no form submission).
- Gate: none. Its result is a precondition for the post approval gate.
- Tools: `post_queue`, Claude API, read-only HTTP.

### 3.7 Approval inbox agent (course: weekly review step)
- Purpose: presents the weekly batch to the human, one screen, grouped by brand and day, with QA results.
- Outputs: approval records (`post`) only when the human decides.
- Gate: `post` (weekly batch). Pamela approves; Frederic is backup.
- Tools: `approvals`, `post_queue.approve_week` (M2).

### 3.8 Publisher agent (course: publish checklist)
- Purpose: publishes approved, due posts to Facebook and Instagram; reports results.
- Sub-agents:
  - Meta publisher (M3, done).
  - Reel uploader (M3, done for IG; FB video path too).
- Gate: the `post` approval already exists; publish runs only for `scheduled` posts.
- Tools: `publisher`, `meta_graph`, media host.

### 3.9 Webinar agent (course: webinar funnel)
- Purpose: runs the T-21 promotion schedule, registration links, reminders, and replay upload.
- Sub-agents:
  - Promo scheduler (creates draft promo posts; they go through the normal post gate).
  - Replay uploader (YouTube private upload, chapters, Calendly CTA in description).
- Gate: `post` for promo posts; `visibility` for public flip of replays.
- Tools: MailerLite (opt-in only), Calendly, YouTube Data API.

### 3.10 Engagement monitor agent (course: management checklist; report only)
- Purpose: reads comments and messages on our own posts; counts, categorizes, flags items needing a reply. Never replies on its own.
- Sub-agents:
  - Classifier (question, complaint, lead interest, spam, other).
  - Escalation flagger (complaints and legal/compliance topics go to a human immediately).
- Gate: none for reading.
- Tools: Meta read APIs (comments; messages once permitted).

### 3.11 Q&A responder agent (course: management checklist → reply step)
- Purpose: drafts replies to classified items from approved answers first, then drafts new ones.
- Sub-agents:
  - Answer retriever (searches previously approved replies and brand facts).
  - Reply drafter (Claude, Haiku default).
  - Reply QA (runs the claims check and link check from 3.6).
- Gate: `reply`. Every reply needs a human approval before sending. No exceptions.
- Tools: Claude API, Meta send API (only after approval).

### 3.12 Insights agent (course: performance review)
- Purpose: pulls post and account metrics on a schedule (read-only).
- Sub-agents: none.
- Gate: none.
- Tools: Meta insights APIs, YouTube analytics.

### 3.13 Weekly report agent (course: weekly review)
- Purpose: builds the weekly report: plan vs. published, QA failures, reply backlog, metrics, token expiry warnings.
- Sub-agents: none.
- Gate: none (report is local).
- Tools: `reports` table, local file output.

### 3.14 Boost advisor (course: boost step; your rule: spend only after view milestones)
- Purpose: when a post passes a view milestone (300, 1,000, 10,000), recommends a boost with a suggested budget and audience.
- Gate: `boost`. Nothing spends without approval. Daily cap set in config.
- Tools: insights agent output; Meta ads API (not built yet).

### 3.15 Lead research agent (course: lead pipeline)
- Purpose: imports the NE Florida list (CSV/XLSX), finds public registry records, extracts company website emails, and records their source.
- Sub-agents:
  - Source fetcher (only sites whose robots.txt and terms allow it; rate-limited).
  - Email extractor (from company pages only, with source URL).
  - Email verifier (syntax, MX lookup, free verifier if available).
- Gate: `found → qualified` needs a human decision (qualification gate).
- Tools: file import, HTTP GET (respecting robots.txt), DNS.

### 3.16 CRM sync agent (course: CRM stages)
- Purpose: mirrors lead and booking stages to Google Sheets, one-way from the database, plus manual stage edits pulled back.
- Gate: none for sync; stage changes that affect outreach are logged.
- Tools: Google Sheets API.

### 3.17 Outreach agent (course: cold sequence)
- Purpose: drafts the first email and follow-ups, schedules day 0, day 4, day 10 sends, enforces the 30/day cap, and stops on reply.
- Sub-agents:
  - Email drafter (Claude; includes sender identity, postal address, unsubscribe line).
  - Footer checker (refuses send if any required element is missing).
  - Reply classifier (detects replies and opt-outs; stops the sequence).
- Gate: `cold_send` for first send per contact. Follow-ups run only for approved contacts.
- Tools: Microsoft Graph (`fred@clasbyfred.com`), CRM sync.

### 3.18 Reminder agent (course: live and manual tasks)
- Purpose: sends reminders for Facebook Live, webinar, and manual tasks (Reaction Follow Invites, Favorite Follow, Influencer Inner Circle as checklists only).
- Gate: none (reminders only).
- Tools: local notifications.

## 4. Gates (approval kinds)

| Gate | Who approves | Used by | Frequency |
|---|---|---|---|
| `post` | Pamela (Frederic backup) | Approval inbox, webinar promo | Weekly batch |
| `reply` | Pamela | Q&A responder | Per message |
| `cold_send` | Frederic | Outreach agent | Per contact, first send |
| `boost` | Pamela | Boost advisor | Per boost |
| `visibility` | Pamela | Replay uploader | Per video |
| `connect` | The account owner (their own Mac) | Connect flows (Canva, Meta, Google, Microsoft) | Once per account |
| `qualification` | Frederic | Lead research | Per lead, found → qualified |

## 5. Shared runtime

- Scheduler: launchd every 5 minutes runs `socialops run-due`, which calls the orchestrator. The orchestrator calls agents in a fixed order and stops on the first gate it cannot pass.
- LLM calls: Claude API, model per agent in config. Default `claude-haiku-5-5`. Caps: per-run draft limit (M2B) and a daily token budget (to add).
- State: SQLite per Mac (existing). New tables: `agent_runs`, `qa_results`, `replies`, `contacts`, `outreach_steps`, `opt_outs`, `insights`, `reports`, `leads`, `lead_sources`, `webinars`.
- Secrets: Keychain only (existing).
- Dry-run: global default; each agent respects it.

## 6. Packaging: .dmg for other Macs

- Build: PyInstaller or py2app bundle of the Python app plus `brands.toml` template.
- Installer: a `.dmg` containing the app. Drag to Applications.
- First run: setup wizard creates the database, asks for brand config, and walks through the connect flows. Each person connects their own accounts on their own Mac.
- Signing: Developer ID signing and notarization are required so other Macs don't block the app. This needs an Apple Developer Program membership ($99/year).
- Updates: manual `.dmg` replacement for now. Auto-update is later.

## 7. Build order (proposed)

1. Agent runtime: `agent_runs`, orchestrator, gate checks, dry-run wiring (M4a).
2. QA agent with rule checks and link checker (M4b). This is the "automated Q&A step" from the course checklist.
3. Approval inbox: weekly batch screen (M4c).
4. Publishing loop tied to the scheduler, including reels (M4d).
5. Engagement monitor and Q&A responder, with reply gate (M5).
6. Webinar agent, YouTube replay upload (M6).
7. Visual agent, Canva fill and export (M7).
8. Video queue, Zoice (M8).
9. Insights and weekly report (M9).
10. Lead research, CRM sync, outreach (M10–M12).
11. Boost advisor (after milestones are reached).
12. Packaging: `.dmg` build, signing, first-run wizard (M13).

## 8. Open items

- Apple Developer account for signing (needed for step 12).
- Meta permission to read and send messages and comments: needs app review for anyone outside app roles. For you and Pamela only, dev-mode roles may be enough. Confirm when we reach step 5.
- Zoice API still unconfirmed (step 8 runs manually until then).
- Canva autofill on Pamela's plan (step 7).
