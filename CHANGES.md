# Script Factory - this round of changes

Copy these files over your project (the `data/` folder is NOT included, keep yours). The only schema change is a new
`supervisor_languages` table, created automatically on startup (CREATE TABLE IF NOT EXISTS). Verified against a copy of data/factory.db.

| Request | What changed |
|---|---|
| KPI cards clickable | Every KPI (language cards, language view, agent view) opens a searchable table of the rows behind it. New endpoint `/admin/kpi_detail`. |
| Remove 1503 everywhere | Gone from dashboard, add-language form, home, Excel report, README. Progress = completed / total scripts. Generation planner quota (9 x 167) intentionally untouched. |
| All languages selection | `?lang=all` on Agents, Scripts, Pairs & Assignment, Reports, Client Access. Multi-language supervisors get "All my languages". |
| Agents tab, "Paired with" | Shows "N pairs"; hover shows every partner name. |
| Multiple pairs (manual only) | A person can be in many pairs. 15h personal cap is shared across all their pairs; 7.5h per pair unchanged. Auto-pair still 1 pair each. |
| Manual pairing swap bug | Re-pairing an existing pair used to drop it, release its scripts and recreate it (A/B flipped). Now rejected with "already paired". |
| Equal distribution | `ops.assign_fair` hands out round-robin. 35 scripts / 2 pairs -> 18 + 17. Used by admin and supervisor assign. |
| Script release bug | UI said "released successfully" even when the server refused. Now shows the real error. Also a supervisor could release another language's pair; now blocked. |
| Reports action buttons bug | `review_report` required `isinstance(actor, dict)` but real sessions were DB rows, so Accept/Reject was refused for real users (only tests passed). Fixed; errors now shown instead of a 500; bulk review reports skipped rows; supervisors limited to their languages. |
| Agent time bug | `mm:ss` could render "02:60". Now computed from total seconds (7 places + Excel). |
| Supervisor with multiple languages | Admin > Supervisors: language checkboxes on create, "Languages" button to edit. Sidebar language picker. |
| Combine Pairs + Assignment | One tab "Pairs & Assignment" (old /admin/assign URL still works). |
| Admin profile menu | Click the profile card: Admin view / Supervisor view / Agent view. Agent view = picker, with a "Back to my view" button. |
| Client URLs per language | Each link has a language; add as many as you like per language. Agents only see their language's links. Supervisors can only manage their own languages' links. |
| Dashboard for all supervisors | `/admin` dashboard now open to supervisors, scoped to their languages (no add/delete language). |
| Agent dashboard | Partner panel lists every partner with script count (pending/completed). Click one -> only that pair's scripts. Script rows show "with <partner>". |

Tests: 32 pass (3 old tests encoded the old rules and were rewritten: manual pair dropping the old pair, 1503 hidden spans, supervisor /admin = 403).

Line endings: app files were converted from CRLF to LF while editing.

---

# Round 4

| Request | What changed |
|---|---|
| 8-minute script showed 0.1 h | Hours were rounded to 1 decimal. Engine now keeps 2 decimals and the UI shows `8 min`, `1h 05m` (new `hm` filter) on Agents, Pairs and Dashboard. |
| View Photo not working | Photos only lived on Render's disk, which is wiped on redeploy. They now go to a private Supabase bucket (`proofs`) with a local cache, and `/uploads/proof_*` serves from either. Proof photos now require login (agents only see their own). |
| Assign to all active pairs | Moved to a side panel next to the Scripts cards, with a live split preview (e.g. 15 per pair, 3 pairs, 20 in stock -> 7 / 7 / 6). |
| Agents: sheet link + local upload | *Import Agents* dialog: Google Sheet link with optional auto-sync every N minutes, or .xlsx/.csv upload; column format shown; template download; existing agents are updated, never deleted. The old "Sync from Sheet" button actually pushes data OUT, so it is now called "Push to Sheet". |
| Pairs bulk upload | Format shown (agent_a, agent_b, availability), accepts names, phones, emails or codes, .xlsx/.csv, and lists every rejected row with the reason. |
| Bulk upload imported 0 rows | The forms posted a field called `file` but the server read `csv_file`. Fixed for agents and pairs. |
| Images in Excel / Sheets | Excel report embeds each proof as a thumbnail inside its cell. Google Sheets gets a *Submissions* tab using `=IMAGE(url)` with a signed Supabase URL. |
| Passwords in README / code | Removed from README and from `ops.py` (a hard-coded login bypass and a reset-on-every-start). Admin/dev passwords now come from `SF_ADMIN_PASSWORD` / `SF_DEV_PASSWORD`; the old defaults are rotated once on first start. |
