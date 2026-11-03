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
- Cold email is Microsoft 365 only, from `fred@classbyfred.com`. Opt-outs and the footer are required.

## Caption drafting (M2B)

```bash
python -m socialops edit-post --id 1 --subject "HIPAA risk assessment basics" --visual-source canva
python -m socialops draft-week --brand clas_by_fred --week 2026-11-02          # dry-run: no model call
python -m socialops --live draft-week --brand clas_by_fred --week 2026-11-02   # calls Claude; costs money
```

- Live drafting reads the key from Keychain: `security add-generic-password -s socialops.claude -a api_key -w`. Do not paste the key into a command you keep in history.
- Drafts never change status. Approval is still required (`approve-week`).
- Model: `[llm] model` in `config/brands.toml`. Per-run cap: `max_drafts_per_run`.
