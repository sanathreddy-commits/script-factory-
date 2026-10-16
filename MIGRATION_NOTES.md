# ScriptFactory — Postgres migration & deploy notes

## What changed
- **Primary database is now Supabase Postgres** (durable; survives Render sleeps and redeploys).
  SQLite remains the automatic **fallback** when no Postgres URL is set (local runs).
  Implemented as a dialect-translating layer (`app/pgcompat.py`) so every existing query works unchanged.
- **10-minute keep-alive** self-ping (`/healthz`) so Render free tier does not idle-sleep.
- **App ships with zero scripts** — upload incrementally (each ~18–23 KB). Dedup now blocks
  true duplicates (exact content hash + 70% shingle overlap) but **no longer drops distinct scripts**
  (the buggy "identical opening line" check was removed — it was silently dropping real scripts).
- **Supervisor Edit button fixed** (modal + save route were missing).
- **Admin → "View"** on each supervisor row opens that supervisor's own view; "Back to my view" returns.

## Render environment variables
Set these (Render → service → Environment):

| Key | Required | Value |
|-----|----------|-------|
| `SUPABASE_POSTGRES_URL` | **Yes** | Supabase **pooler** URI (Settings → Database → Connection Pooling, mode *Transaction*, port 6543): `postgresql://postgres.<ref>:<DB-PASSWORD>@aws-0-<region>.pooler.supabase.com:6543/postgres` |
| `SF_ADMIN_PASSWORD` | **Yes** | admin login password |
| `SF_DEV_PASSWORD` | **Yes** | developer login password |
| `SUPABASE_URL` | for proof photos | `https://<ref>.supabase.co` |
| `SUPABASE_KEY` | for proof photos | **service_role** key (server-side only) |
| `SUPABASE_PROOF_BUCKET` | optional | default `proofs` |
| `SF_ADMIN_CODE` | optional | fixed 8-char access code (else regenerated each boot) |
| `RENDER_EXTERNAL_URL` | no | injected by Render automatically — keep-alive uses it |

**User IDs are NOT env-set** — they are fixed in code: admin = `admin@awign.com`, developer =
`sanath.reddy@awign.com`. You only set the *passwords*.

## One-time Supabase setup
1. Create a Supabase project. Copy the **pooler** connection string into `SUPABASE_POSTGRES_URL`.
2. The app creates all tables automatically on first boot (`init_db`). No manual SQL needed.
3. For proof photos, set `SUPABASE_URL` + `SUPABASE_KEY` (service_role). The `proofs` bucket is auto-created.

## Capacity
Postgres handles incremental uploads with no full-file re-sync. At ~20 KB/script, 9,000 scripts is
~180–400 MB of row data — comfortable on Supabase Pro (8 GB). Supabase **free** Postgres is 500 MB;
fine for a few thousand scripts, upgrade before you approach that.

## Known / not-built (flagged, not changed)
- **No per-row "exported" flag.** Sheet export produces an on-demand `.xlsx`; the app does not track
  which rows were already exported. Ask if you want this added.
- Dedup near-duplicate scan is O(n) per upload against the existing library — fine incrementally,
  grows slowly as the library gets large. Can be optimized later if needed.

## Update 2 (this build)
- **Multi-device logins:** the one-device restriction is removed entirely. Any login (agent,
  supervisor, admin) works on unlimited devices at once; each login is its own session.
- **Admin "View as supervisor"** now mirrors "View as agent": a picker of supervisors
  (Nav > profile > "Supervisor view") → click "Open supervisor view" → you see exactly what that
  supervisor sees, with a "Back to my view" button to return. (Per-row "View" on the Supervisors
  page still works too.)
- **Assignment is language-locked (verified, not a bug):** with language = All, Pairs & Assignment
  shows every language's pairs, but assigning only ever gives a pair scripts of *its own* language
  (`pick_script` filters `WHERE language_id = pair.language_id`; the execute route groups pairs by
  language first). A Kannada pair can never receive a Tamil script.

## Update 3 (this build)
- **Reports export filters:** the Reports tab now has From date / To date / Language controls that
  drive both "Download Excel" and "Export CSV". Leave dates blank for all time. Supervisors are
  auto-scoped to their own languages.
- **Clean, professional export:** the download is rebuilt to match the team's manual tracker — a
  "Sessions" sheet (S.No, Date, Language, Topic, Script Code, User 1 (Host), User 2 (Guest),
  Duration mm:ss, Status, Review, Comment/Invalid Reason, Proof) with styled frozen header,
  auto-filter and zebra rows, plus a per-language "Summary" sheet. Columns the app does not capture
  (free-text Observation, Type-of-issue taxonomy, Resolution status, Project) are not invented.
- **Easy email setup (Dev page > DV1 Connections > "Email Delivery Setup"):** pick provider, paste
  key, set sender, Save, then "Send test email". Auto-detects Brevo/Resend/SendGrid.
- **Gmail-works-local-but-fails-on-Render — explained and fixed:** Render Free blocks outbound SMTP
  ports (25/465/587), so Gmail SMTP times out there (it only uses those ports); your laptop allows
  them, which is why local worked. Fix built in: use **Brevo** (free, 300/day) or Resend — they send
  over HTTPS port 443, which Render allows. Get a Brevo key (starts `xkeysib-`), paste it in the
  setup card, verify your sender in Brevo, send a test. (If you must keep Gmail, upgrade Render to a
  paid instance to unblock port 587.)

## Update 4 (this build)
- **Proof capture = camera** (already `capture="environment"` — opens the phone camera, not a file picker).
- **Photos auto-compressed to ~100 KB JPEG** on submit (Pillow) before storing — big storage saving.
- **Primary image store = Google Drive** (15 GB free): the Drive link is saved on the row
  (`assignments.proof_drive_url`) and shown as a clickable **"View photo"** link in the Excel/CSV export.
  A **safety copy stays in Supabase Storage** (so the in-app popup always works, even if Drive is down).
- **In-app proof view = popup** (already present: "View Photo" opens a modal with the image).
- **Easy Drive config** in Dev page > DV1 Connections > "Google Drive Storage Setup" (folder ID +
  service-account JSON + Test Drive). Needs a Google service account shared on the target folder.
- **Duration entry split into Minutes + Seconds; seconds default to 0** (so "7" = 7:00, no error).

## Update 5 (this build)
- **BUG FIX — Pairs & Assignment "Scripts" view:** was 500 (query didn't select proof_img). Fixed; the pair-scripts modal loads.
- **BUG FIX — Dashboard drill-down:** clicking a language card's number now shows THAT language (cards carry data-kpi-lang); previously used the global selector.
- **Assign-to-all box:** compact — Pairs / Available scripts / Each pair gets; paragraph removed.
- **"+ Add Language"** removed from the Management menu (still available via the language dropdown).
- **Same person in different languages:** uniqueness is now per-language (phone/email can repeat across languages, not within one). Index migrated automatically.
- **Send access codes:** "Send Code to All" + new "Send to Unsent" (only people never sent one). Each send stamps code_sent_at.
- **Import Agents modal:** cleaned up (big required-column table removed); Excel + CSV template download buttons.
- **Client Access:** training video now accepts a Google Drive link (auto-converted to preview embed); new "Bulk add" (paste language, taxonomy, url, code per line).
- **Scripts folder view:** 📁 Folders / ☰ List toggle. Folders browse language → domain → sub-domain → specialisation with per-folder counts and status; drill in via breadcrumb; List is the flat table. (Client-side; needs a real browser to click through.)
- Note: the unused legacy `app/templates/admin_scripts.html` is not the live scripts page (left as-is).

## Update 6 (Production build)
- **Pairs page layout:** the "Assign to all" card now sits at the top beside BOTH KPI rows (Agents & Pairs + Scripts) — no more empty gap.
- **Shared Google spreadsheet for agents + pairs:** in Import Agents > Google Sheet link, set a "Pairs tab name" (same spreadsheet). On sync, agents import first, then pairs from that tab. Auto-sync does both.
- **Rule enforced:** a person must already be an agent to be paired — pair rows naming an unknown agent are rejected (e.g. "Agent B 'Ghost Person': not found"). This was already true for file upload; now also for the shared sheet.
