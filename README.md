# ScriptFlow (Script Factory)

High-performance operations and recording management system for multi-language speech datasets: manages scripts per language across Healthcare and BFSI subdomains, manages agent pairing with strict 7.5h joint limits and 15h personal caps, tracks recording sessions with photo proofs, provides automated cloud backups, Google Sheets synchronization, and multi-sheet Excel reporting.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the local server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Run the test suite
python -m pytest tests/
```

Access the application in your browser:
- **Developer Panel**: `http://localhost:8000/dev/login`
- **Admin Dashboard**: `http://localhost:8000/admin/login`
- **Supervisor & Agent Hub**: `http://localhost:8000/login`

---

## Authentication & Role Architecture

| Role | Landing Page | Auth Method | Credentials | Access Level |
|---|---|---|---|---|
| **Developer** | `/dev/connections` | Email & Password | Email is the developer account; password comes from the `SF_DEV_PASSWORD` environment variable | API keys, cloud sync, developer settings |
| **Admin** | `/admin` | Email & Password | Email is the admin account; password comes from the `SF_ADMIN_PASSWORD` environment variable | Full system control, 3-level dashboard, assignment overrides |
| **Supervisor** | `/team` | 8-Char Code | Base32 access code issued by an admin | Language-specific pairing, review queue, agent monitoring |
| **Agent / Speaker** | `/me` or `/agent` | 8-Char Code | Base32 access code issued by a supervisor/admin | Recording workspace, dialogue viewer, proof upload, session history |

**No password or key is ever stored in this repository.** Secrets live only in environment variables on the host
(Render > Environment). If `SF_ADMIN_PASSWORD` / `SF_DEV_PASSWORD` are not set, a random password is generated on first
start and printed **once** in the server log; set the variable to choose your own.

### Environment variables

| Variable | Purpose |
|---|---|
| `SF_ADMIN_PASSWORD`, `SF_DEV_PASSWORD` | Admin / developer login passwords |
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_KEY` | Supabase **service_role** key (server side only) |
| `SUPABASE_BUCKET` | DB backup bucket (default `factory-data`) |
| `SUPABASE_PROOF_BUCKET` | Proof-photo bucket, created private automatically (default `proofs`) |
| `SF_ADMIN_CODE` | Optional fixed admin access code |

Proof photos are stored in the private Supabase bucket (with a local cache), so they survive Render redeploys.

## Importing agents and pairs

- **Agents** page > *Import Agents*: paste a Google Sheet link (optional auto-sync every N minutes) or upload `.xlsx`/`.csv`.
  Columns: `name`, `phone`, `email`, `gender`, `availability`, `language` (see the dialog for which are required).
- **Pairs** page > *Bulk Upload Pairs*: `.xlsx`/`.csv` with `agent_a`, `agent_b`, `availability`.
- Templates: `/admin/template/agents.csv`, `/admin/template/pairs.csv`.

---

Access codes are 8-character Base32 strings with Luhn-mod-32 check characters that catch single-character typographical errors automatically.

---

## 3-Level Dashboard Hierarchy

```
Level 1: All Languages Grid (/admin)
  │   └── Total Scripts, Assigned, Pending, Completed, Recorded & Accepted Hours (Progress = completed / total scripts)
  │
  ├──► Level 2: Language Drill-Down (/admin?lang_id=<id>)
  │      └── Registered Agents Table: Sl. No., Name, Assigned, Pending, Completed, Recorded & Accepted Hours (15h cap)
  │
  └──► Level 3: Individual Agent Work History (/admin?lang_id=<id>&agent_id=<uid>)
         └── KPI cards + Full history of all scripts worked on, dialogue view, partner name, time taken, status badge
```

---

## Core System Invariants & Enforcement

1. **Unified Hours Engine (`ops.get_hours`)**:
   - Single source of truth across all routes, dashboard metrics, and cap enforcement.
   - Computes recorded, accepted, pending, rejected, and planned minutes/hours.
   - Enforces the 15h (900m) personal cap and 7.5h (450m) joint pair cap.
   - Planned batch scripts (`ASSIGNED`, `IN_SESSION`) count toward committed time to prevent concurrent cap bypass.

2. **Pair Immutability & Re-pairing Guard (`ops.can_pair`)**:
   - Re-pairing two agents who reached the 7.5h joint work cap is blocked across all paths (`propose_pairs`, `create_pair`, `auto_pair`, `manual_pair`).
   - Assignment history is strictly immutable: `assignments.pair_id` is never rewritten upon re-pairing or manual pairing.

3. **Assignment State Machine**:
   - Strict transition guards: reviews are only allowed from `SUBMITTED` status.
   - Idempotent acceptance: double-approvals never insert duplicate ledger rows.
   - Append-only accounting: rejections after acceptance write compensating reversal rows (`kind='REVERSAL'`) to maintain append-only ledger integrity.
   - Agents cannot modify or resubmit recordings once accepted.

4. **Pair Drop Cascade (`ops.drop_pair`)**:
   - Dropping a pair safely releases open scripts back to the available pool with `status='RELEASED'` and resets scripts to `READY`.

5. **Persistence & Clean WAL Checkpoints**:
   - SQLite runs in `WAL` mode with automatic `PRAGMA wal_checkpoint(TRUNCATE)` before cloud sync and on shutdown.
   - Local database is protected against startup overwrite when data already exists.

---

## Automated Test Suite

Run all tests via pytest:
```bash
python -m pytest tests/
```
The suite covers the hours engine, pairing, assignment state machine, web routes and imports.
