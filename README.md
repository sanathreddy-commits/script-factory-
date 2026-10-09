# ScriptFactory

Web app for the script project: plans 1,503 scripts (9 sub-domains × 167), generates and validates them, pairs people, assigns scripts under hour caps, tracks sessions, reconciles with the client's data. One codebase: wide sidebar layout on a laptop (admin), stacked cards and big touch targets on a phone (team lead, participants).

## Run it

```bash
cd scriptfactory
./run.sh                 # installs deps, starts on http://localhost:8000
```
First start prints the **admin access code** (also saved as `data/ADMIN_CODE.txt`). Phones on the same Wi-Fi open `http://<laptop-ip>:8000`.

Try it with fake data first: `python seed_demo.py` (then `./run.sh`). It plans Kannada, adds *stub* writer/judge keys, queues Wave 1, and prints codes for a lead, a reviewer and 8 participants.

Tests: `python -m pytest tests` (20 pass).

## How people use it

| Who | Sees | Logs in with |
|---|---|---|
| Admin (laptop) | Dashboard, language setup, waves, key pool, review, people, pairs, assignments, data/reports, settings, audit | access code |
| Team lead | Own language only: exceptions, people, pairs, assignments, reports | access code |
| Reviewer | Review queue of own language | access code |
| Participant (phone) | Own hours, partner, script queue, recording screen | access code |

Access codes are 8 characters, stored as plain text (as you asked), with a check character so typos are caught. Admin or the language's team lead can view, share (WhatsApp button) or reissue a code; every view is logged.

## Flow

1. **Admin → New language** (quota table is pre-filled 167 × 9 = 1,503; the form rejects a target that doesn't match).
2. **Plan slots.** Code decides scenario, roles, names, outcome, tone, register, code-switching, background, opening and every figure. Consultation split is 84/17/25/25/8/8.
3. **Generation → add keys** (writer + judge, different models). **Release Wave 1** (100 scripts, stratified, 100% human review). Later waves open only after the previous one is fully reviewed.
4. **Review.** Accept / fix / reject with reason. If the reject rate in the last 30 reviews exceeds 8%, generation pauses automatically.
5. **People → add or bulk-upload CSV**, share codes. **Pairs → suggest pairs** (availability overlap, never a repeat partner). **Assignments → assign.**
6. Participants: Ready (both) → Start → record on the client page → Done (both). Unverified completions are labelled UNVERIFIED everywhere.
7. **Data & reports:** upload the client session log to reconcile, QC results, ready-made script import, CSV downloads (scripts, people, pairs, assignments, ledger, cost, audit, client manifest, payout).

## Rules enforced by the database (tested)

- A script cannot have two active assignments (unique index; 12 concurrent assigners tested).
- Only APPROVED scripts are assignable; assigned version never changes.
- Confirmed + planned hours never exceed pair/person caps (admin override needs a reason, logged).
- Output that fails validators is never stored; failures go to NEEDS_HUMAN.
- Audit log and hours ledger cannot be edited or deleted (triggers).
- Gender-encoded scripts only go to a pair whose genders fit (when genders are set).

## Not built yet (be aware)

- **Real LLM calls are untested.** The Anthropic and OpenAI-compatible adapters are written but I could only test the pipeline with the `stub` provider, whose output is random placeholder text in the right script. Run the 20-script pilot with a real key before trusting quality or cost. Stub scripts are marked TEST and cannot be assigned unless "allow test scripts" is on in Settings.
- Gate 0 is still unanswered, so defaults apply: verification = self-report (UNVERIFIED), read-aloud mode, duration 60 s to 60 min. Change them in Settings/language page once the client answers.
- No email (WhatsApp click-to-chat links instead), no calendar scheduling/reminders/no-show tracking.
- No Google Drive backup or Sheets mirror (needs Google credentials). Approved scripts are backed up to `data/backups/`, and the database is copied nightly to `data/db_backups/`. CSV exports replace the mirror.
- Similarity gate is word 5-gram overlap plus opening-line uniqueness. No embedding check.
- Required-vocabulary is checked by the judge and the reviewer's checklist, not by code (needs a per-language vocabulary map).
- API keys are stored in the database as plain text (masked in the UI). Per-language isolation is enforced in the app layer, not by database row-level security.
- Reports are CSV, not XLSX.
- SQLite is fine for the pilot (about 100 concurrent users). Move to Postgres before several languages run at once.
- Admin sign-in is a code only. Put the app behind HTTPS before exposing it to the internet.
- Consent text is a draft. Compliance must approve it. Briefs are built by code, not by an AI call.
