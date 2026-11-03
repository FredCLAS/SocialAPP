# Open Questions (round 3)

## Resolved

| # | Decision |
|---|---|
| Q1 | Cold email: Microsoft 365 on owned domain. Send via Microsoft Graph. |
| Q2 | Canva: Connect API access. Pamela uses her own Canva profile. |
| Q5 | Canva authorization by Pamela on her Mac. `visual_engine` runs on Pamela's Mac only. |
| Q6 | Pamela's Mac posts for both brands and runs the lead engine. Frederic is backup. |
| Q7 | Posts: weekly batch review by Pamela. |
| Q8 | Target lists: CSV or XLSX. |
| Q9 | Company owns its domain. |
| Q10a | Footer address: 6271 St Augustine Rd Ste 24, Jacksonville, FL 32217, US. |
| Q12 | Pamela manages Meta Business Center, Pages, and Instagram. |
| Q13 | YouTube replays go to the Class by Fred channel (verified). |
| Q14 | Posting times: 08:30 and 10:00 Eastern. |
| Q15 | First live cold send per contact needs approval. Cap 30/day. |
| Q18 | Canva plan includes PNG export. Autofill still to verify before M5. |
| Q17 | Boost ladder: recommend boost at >300, >1,000, >10,000 views. Each boost needs Pamela's approval. Never automatic. Off until the first post passes 300 views. |

## Still open

| # | Question | Why it matters | Blocker for |
|---|---|---|---|
| Q10 | **CLAS cold-email sender.** Which mailbox sends CLAS cold email? Options: `fred@classbyfred.com` or `thread@clasbythread.com`. Note the brand domain is `classbyfred.com`. Confirm the spelling and whether you own `clasbythread.com`. Pamela's art brand has no cold email, so `pam@pamelapatton.art` is not used for outreach. | Sender identity and footer. Mailbox must exist in Microsoft 365. | M9 |
| Q11 | **KB PDF extraction.** Run on your Mac: `cd ~/Desktop/CLASbyFRED/Tools/KB && for f in *.pdf; do echo "== $f"; pdftotext -layout "$f" - \| wc -c; done`. Or upload the folder here. | Phase A step 1. | Phase A completion |
| Q12a | **Meta Pages and IG linkage.** Confirm each brand's Facebook Page and IG account is linked to the Business Center under Pamela. | OAuth and permissions. | M3 |
| Q3a | **Zoice account.** Pamela creates it tomorrow. Then check API docs and pricing. | M6. | M6 (manual queue works without it) |
| Q18a | **Canva autofill.** Confirm Connect API autofill on Pamela's plan. PNG export is confirmed. | `visual_engine` fill step. | M5 |

## Notes

- Footer and sender identity are config values, not secrets. Store in `brands.toml`.
- No credentials in this document. Do not paste tokens or passwords into chat.
