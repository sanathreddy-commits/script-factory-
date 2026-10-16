<USER_REQUEST>
ok use what ever model reqired and search the internet and find which model is good in which and use that to complete the project :and see that and for plannig design Frontend everything  as i will give open router key "<OPENROUTER_API_KEY>"  there are top models which are best in there thing and we have opus , fable , gpt astra and so use them and complete the project plz

claude gave me this to use for development of this project if useful consider 
"# SCRIPTFLOW: MASTER PROMPT (v2, raw)

Paste PART 0 to PART 3 first, then each page group in PART 4 one at a time. Every page inherits PART 1 to 3.

---

## PART 0. PRODUCT

ScriptFlow manages two-person role-play voice recordings in 6 languages: Kannada, Tamil, Gujarati, Punjabi, Marathi, Odia. Agents record on a SEPARATE client portal. ScriptFlow only stores scripts, makes pairs, assigns scripts, collects time taken + a photo of the successful submission, tracks hours, and records the supervisor's review. Do not design a recording screen, chat, payments or analytics.

Roles: Developer > Admin > Supervisor (one language only) > Agent.
Script ID format: KN-HC-CONS-001. Taxonomy display: Domain > Sub-domain > Specialty.

---

## PART 1. DESIGN CODE (raw, internal)

**Style: plain and functional. Do NOT beautify.**
- Flat. No shadows, no gradients, no illustrations, no animations, no decorative icons, no empty-state artwork. Icons only for copy, search, menu.
- Borders 1px #DCEDEC. Radius 4px. 4px spacing grid.
- Font: system-ui. Script text: Noto Sans for the script's language, 18px minimum, line-height 1.7.
- Text sizes: page title 22, body 16 (mobile) / 14 (desktop), table 14.

**Colours**
- Text #16292A, muted #5E7877, page background #F4F9F8, cards #FFFFFF.
- Primary (buttons, active tab, links) #00A896. Sidebar #032E30 with white text.
- Status shown as plain coloured text with a 1px border, no fill: Available grey #5E7877, Assigned/Pending amber #B7791F, Submitted #028090, Accepted green #0B4D3E, Rejected red #D64545, Limit reached red #D64545.

**Components**
- KPI card: number + label, plain border.
- Tables: sticky header, 40px rows, search + filter above, pagination below, row actions at the right.
- Buttons: primary (filled teal), secondary (outline), danger (red). 48px tall on mobile, 40px on desktop.
- Delete always opens a modal with a mandatory Reason field.
- Progress bar for limits: teal under 80%, amber 80-99%, red at 100%.

**Layout**
- Agent app: mobile only, 390px, two tabs at the bottom (Dashboard, Scripts), 48px tap targets.
- Admin / Supervisor / Developer: desktop-first, left sidebar 240px, top bar. On mobile the sidebar becomes a drawer and tables become stacked rows.

---

## PART 2. RULES

**R1. Script lifecycle**
Available -> Assigned -> Submitted -> Accepted. If rejected, the script goes back to Available (the unassigned pool) and can be assigned again.

**R2. Review**
Supervisor or Admin marks each submission Accepted or Rejected, in the web app or in the linked Google Sheet (same record, latest change wins). It happens 1 to 4 days after submission, so every submission shows "days since submitted". Reject reason is optional.

**R3. Session time**
Time taken is 5 to 15 minutes. Outside that range the input shows the error "Time must be 5 to 15 minutes" and Submit is blocked.

**R4. Hours: Recorded and Accepted always side by side**
Everywhere hours or time are shown (language cards, agent rows, pair rows, assignment rows), show **Recorded** and **Accepted** next to each other. Recorded = all submitted sessions. Accepted = accepted sessions only.
- Pair limit: 7.5 h. Display: "Recorded x / 7.5 h | Accepted y h". The same pair cannot continue after the limit; status becomes "Limit reached", no more scripts, and it cannot re-form.
- User limit: 15 h. Display: "Recorded x / 15 h | Accepted y h". A user at the limit is excluded from pairing and assignment.
- The limit bar is driven by Recorded.

**R5. Pairs**
Only active agents with hours left are eligible. Match by availability (Morning / Afternoon / Night / All-day), prefer opposite genders. Pair history is kept. Delete pair needs a reason.

**R6. Scope**
A Supervisor sees and edits only their language, everywhere.

**R7. Logins**
- Developer: login ID + password. No 2FA.
- Admin: login ID + password, given by the Developer. No 2FA.
- Supervisor: access code.
- Agent: access code, then a "Start Working" button. No consent screen anywhere.

---

## PART 3. OUTPUT RULES

- Desktop frame (1440) and mobile frame (390) for every Admin, Supervisor and Developer page. Agent pages are mobile only.
- Realistic sample data: script IDs like KN-HC-CONS-001, first-name-only agents, phone numbers like 98XXXXXX01.
- Do not add any page, field, button or text that is not listed below. Agent pages especially: nothing extra.
- Name each screen with its page ID (L1, AG1, AD4...).

---

## PART 4. PAGES

### GROUP L. LOGIN
- **L1 Developer:** login ID, password, Login.
- **L2 Admin:** login ID, password, Login.
- **L3 Supervisor:** access code, Login. After login, the top bar shows the language name.
- **L4 Agent (mobile):** one field "Access code" and a "Start Working" button. Wrong code shows "Invalid code".

### GROUP AG. AGENT (mobile, bottom tabs: Dashboard | Scripts). Only the items below.

**AG1 Dashboard**
- 3 KPI cards: Total Assigned, Pending, Completed.
- Below: pair details (partner name, phone) along with team lead (name, phone).
- Training Video.
- Client access URL and codes.

**AG2 Scripts**
- Two lists: Completed, Pending.
- Each row: Script code, Taxonomy, Status.

**AG3 Script**
- Tap a script to open it. Show the script as stored.
- Last 2 boxes: (1) time taken to complete the script, as shown in the client dashboard (mm:ss); (2) photo of the successful submission.
- Submit button.

**AG4 Completed**
- After Submit: Script code, Taxonomy, Time taken, Pair details (partner name).

### GROUP AD. ADMIN (desktop)
Sidebar: Dashboard | Management (Agents, Scripts, Pairs, Scripts Assignment, Reports) | Supervisors.

**AD1 Dashboard**
- All languages as cards: name, agents, assigned, pending, completed, Recorded and Accepted time side by side. "Add Language" button.
- Click a language -> agents working in that language (name, pair, assigned, pending, completed, Recorded and Accepted).
- Click an agent -> their scripts: Script code, Taxonomy, Time, Status (Submitted / Accepted / Rejected).

**AD2 Agents**
- Table: Sl No | Access Code (copy icon) | Name | Language | Phone | Email | Gender | Availability | Paired With | Assigned | Completed | Pending | Recorded x / 15 h | Accepted | Actions (Edit, Delete, Active/Inactive, Access Code).
- Buttons: Bulk Upload Agents, Send Access Code to All, Add Agent (manual form).

**AD3 Scripts**
- KPI row: Available, Assigned, Pending, Completed, Accepted. Button: Sync Scripts.
- Table of scripts by taxonomy and status. Click a script to view details below the table.

**AD4 Pairs**
- Buttons: Auto Pair, Pair Manually, Bulk Upload Pairs.
- Table: Agent A | Agent B | Availability | Assigned | Completed | Pending | Recorded x / 7.5 h | Accepted | Status (Active / Limit reached) | Delete (reason).

**AD5 Scripts Assignment**
- Top: number of scripts per pair (with "Max assignable: N" helper) and an Assign button.
- Table: Pair | Assigned | Pending | Completed | Recorded x / 7.5 h | Accepted.

**AD6 Reports**
- Table: Script code, Pair, Agents, Language, Taxonomy, Time taken, Submission photo (click to enlarge), Days since submitted, Status (Submitted / Accepted / Rejected), Accept / Reject buttons (reason optional), Reviewed by.
- Filters: language, status. Bulk: select rows -> Accept selected / Reject selected.

**AD7 Supervisors**
- Table: name, phone, email, language, access code (copy icon), Active/Inactive, Edit, Delete. Button: Add Supervisor.

### GROUP SV. SUPERVISOR
Same as AD1 to AD6 for one language only. No Supervisors page, no Add Language.

### GROUP DV. DEVELOPER (desktop)
Sees every Admin and Supervisor page. Sidebar: Connections | Keys | Admins | Rules | Backups | Logs.

- **DV1 Connections:** Supabase, Google Drive, Google Sheets, Generation machine. Each: status, last tested, Test button, Edit.
- **DV2 Keys:** table (Name, Service, Added on, Status). Add Key: service, label, value. Values are write-only; after saving show only the last 4 characters, with Replace and Revoke.
- **DV3 Admins:** list of Admins. Create Admin: name, login ID, password. Reset password, Suspend, Delete.
- **DV4 Rules:** editable values: session min 5 and max 15 minutes, user limit 15 h, pair limit 7.5 h, client access URL.
- **DV5 Backups:** Backup now, backup list, Restore (confirm).
- **DV6 Logs:** tabs Activity | Errors | Sync | Audit. Filters by date, user. Click a row to see full detail."


</USER_REQUEST>
<ADDITIONAL_METADATA>
The current local time is: 2026-10-12T15:40:53+05:30.
</ADDITIONAL_METADATA>