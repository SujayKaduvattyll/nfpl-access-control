# NFPL Access Control Service

Decides **who may do what on which job**, explains every answer, and keeps a history that can answer
"who could have changed this on the 3rd?".  Python 3.10+ standard library only: **nothing to install**.

## Run it (4 commands)

```bash
python -m nfpl load                      # creates nfpl.db and loads the sample data (data/*.csv)
python -m unittest discover -s tests -v  # 38 edge-case tests
python demo.py                           # the 5-minute walkthrough, scripted (see DEMO below)
python -m nfpl serve --port 8000         # run the API on http://127.0.0.1:8000
```

### Hosting it online (Render, free tier)

Push this folder to GitHub, create a **New → Web Service** on render.com from the repo (it reads `render.yaml`), or set the
start command to `python -m nfpl serve --load --host 0.0.0.0`. There is nothing to install. Open the service URL: `/` is the
check page, `/health` confirms it is up. The host's port is read from the `PORT` variable automatically.

### Ask it a question yourself

**In the terminal** (type a person's name or id, an action and a job):

```bash
python -m nfpl ask priya job.check J006      # one-shot
python -m nfpl ask                           # interactive: it prompts for person, action, job
```

**In the browser**: run `python -m nfpl serve`, then open http://127.0.0.1:8000/ . Pick a person and an action from the
dropdowns, type a job id, press **Check**. You get ALLOWED/DENIED, the reason, and the step-by-step trace. Choose a second
person in "Compare" to see "why can she and he can't?"; **Show jobs waiting** lists a person's queue.

Scale case (500 users, 200,000 jobs) and timings:

```bash
python -m nfpl gen-scale --out data_scale      # ~15 s, writes CSVs in the same format
python -m nfpl bench --data data_scale         # loads them and times the hot paths
python -m nfpl load --data data_scale --db scale.db   # or load it into a DB and serve it
```

Measured on a laptop-class sandbox (SQLite, one process): a typical user's job list page **~2 ms**, a typical user's
"jobs waiting for me" **~18 ms**, a single `authorize` **<0.1 ms**, a Manager's waiting list (all CHECKED jobs + flagged jobs)
**~0.4 s**, full attention recompute after an access change **~0.4 s**, initial load of 200k jobs **~10 s**.

`NFPL_NOW=2026-10-17T10:00:00` fixes the service clock (the sample data is set in Sep/Oct 2026; tests and the demo pin it).

## Files

| Path | What it is |
|---|---|
| `nfpl/engine.py` | The pure `decide()` function and the explanation text. The heart of the design. |
| `nfpl/service.py` | Loading, actions, approvals, access management, queues, history, attention flags. |
| `nfpl/server.py` | JSON API over HTTP. Thin wrappers, no logic. |
| `nfpl/gen_scale.py` | Scale-case generator and benchmark. |
| `data/` | The sample data (CSV), `role_permissions.csv` (the permission matrix, as data), `ACCESS_SUMMARY.md`, `build_v2.py` (how it was generated). |
| `tests/test_edge_cases.py` | 38 tests mapped to the brief's edge cases. |
| `DESIGN.md` | Model, decisions, trade-offs, and an answer to each of the 14 questions. |

## API

All "actor" values are user ids; the service trusts its caller to have authenticated them (see Assumptions).

| Method & path | Purpose |
|---|---|
| `GET /authorize?user_id=&action=&job_id=` | May this person do this to this job? Returns `allowed`, a reason code, a plain-language `reason` and a `steps` trace. Dry-run: not logged. |
| `GET /explain?user_a=&user_b=&action=&job_id=` | "Why can she and he can't?" Side-by-side decisions plus a one-paragraph summary. |
| `GET /users/{id}/access` | A person's access in plain terms (including ended and malformed assignments). |
| `GET /users/{id}/waiting` | Jobs waiting for me (+ approvals waiting for me). Uses the same `decide()` as every action. |
| `GET /users/{id}/jobs?stage=&limit=&offset=` | Jobs this person may see (paged, with total). |
| `GET /jobs/{id}` , `/jobs/{id}/history` | The job; its full audit trail (allowed and denied attempts). |
| `GET /jobs/{id}/who-could-change?date=YYYY-MM-DD` | Who could have changed it that day by the assignments valid then, vs who actually did. |
| `POST /jobs/{id}/actions` `{actor, action, reason?, to_user?, changes?}` | Perform `job.prepare / check / file / edit / cancel / reopen / reassign`. Always audited. May return `PENDING_APPROVAL`. |
| `POST /approvals/{id}/approve` , `/reject` `{actor}` | Second-person approval for filed-job cancel, reopen and privileged access grants. |
| `POST /access/grant` , `POST /access/{id}/revoke` | Manage who has what (Admin only; no self-changes; privileged roles need a Manager's approval). |
| `POST /users/{id}/status` `{actor, status}` | Mark a person absent/active (Admin or Manager). |
| `POST /roles` | Create a role from existing permission names (no engineering). |
| `POST /parser/jobs` , `/parser/jobs/{id}` | What the overnight parser may do: create a CONFIRMED job; update intake notes while CONFIRMED. |
| `POST /admin/expire` | Housekeeping: mark ended assignments EXPIRED and flag affected jobs. |

HTTP status: 200 for a successful call (a dry-run `authorize`/`explain` is 200 even when `allowed` is false), **403 when an action or access change is refused**, 404 unknown route/object, 500 only for server faults (the body is still a DENY).

## The rules in one table

| Action | Who (role + scope matching the job) | Valid from stage | Extra rules |
|---|---|---|---|
| view | Maker, Checker, Client User: every stage in scope. Manager: CHECKED, FILED, CANCELLED, REOPENED, plus jobs flagged "needs attention". Parser: CONFIRMED only. | any | Acting always requires seeing |
| edit / prepare | Maker | CONFIRMED, REOPENED | respects the owner's claim (below) |
| check | Checker | PREPARED | **not the preparer**; respects the claim |
| file | Manager | CHECKED | **not the preparer** |
| cancel | Manager | any open stage | written reason; **filed job: second Manager must approve** |
| reopen | Manager | FILED, CANCELLED | written reason; **second Manager must approve**; cycle restarts (prepared/checked/filed cleared) |
| reassign | Manager | any open stage | target must be able to do the next step |
| create / update intake | Service Account (parser) | none / CONFIRMED | cannot change a job's client/branch/direction/mode |
| manage access | Admin | n/a | never for yourself; granting Manager/Admin/Service Account needs a Manager's approval |

The **claim**: a job can have a `responsible_user_id`. While that person is active *and still able to do the next step*,
nobody else may do it. If they are absent, out of scope, or have a delegation, any qualified person may. Stages that
progress release the claim back to the pool.

## Assumptions (the brief was silent)

1. **Authentication is out of scope.** The API takes a user id; a real deployment puts SSO/API gateway in front of it.
2. **Filing is a Manager step** (third pair of eyes; fits "managers see what comes after Maker and Checker").
3. **Absent means unavailable, not stripped.** Absent users cannot act or see jobs; their assignments stay for when they return.
4. **Managers are scoped to everything** (`ALL`) in the sample data, but the model supports narrower scopes.
5. **Visibility ≠ authority.** A Manager does not see healthy in-progress jobs (CONFIRMED/PREPARED); to take one over they mark the owner absent (which flags the job) or wait for it to reach CHECKED.
6. **Dates**: assignments are valid from `valid_from` to `valid_to` inclusive; ended or revoked assignments stop working immediately (checked on every request, nothing is cached).
7. **Sample data timeline** is Sep to Oct 2026; "today" in the demo and tests is 2026-10-17.

## What I cut (and why)

- **Admin break-glass on jobs.** Admins have no job rights at all. A stuck job is fixed by a Manager (reassign) or by flagging it. A logged break-glass override with a second approver is the natural extension; skipped to keep the guardrails simple and testable.
- **Field-level edit permissions and client-visible redaction.** `edit` only changes `notes`; client users see the same fields as staff.
- **Reject/return to maker** (Checker sends a job back). Easy to add as `job.return`; not needed for the access questions.
- **Reconstructing claims and user status for past dates.** "Who could have changed it on the 3rd" uses assignment validity on that date, ignores personal claims and assumes everyone was present. It says so in the response.
- **Concurrency control beyond a lock.** The server serialises requests with a lock; with multiple server processes you'd add an optimistic `stage`/`version` check on the UPDATE.
- **Real expiry scheduler.** Expiry is *effective* instantly by date; `POST /admin/expire` (run daily) only flips the status and refreshes the attention flags.

## Sample data

Loaded by `python -m nfpl load`: **5 branches, 10 clients, 20 users, 26 assignments, 200 jobs, 506 history events, 1 delegation.**
`jobs.csv` has a `scenario` column that tags the deliberate traps:

| Job(s) | Trap |
|---|---|
| J006 / J007 / J008 | Priya: ALLOW / DENY (export) / DENY (Delhi) |
| J021 | Aarav prepared it and is also an Apex Checker → cannot check; nobody else qualified → flagged for a Manager |
| J017 | Historic self-check (Aarav prepared *and* checked): the kind of breach the old system allowed |
| J016, J022 | Held by absent Aditya; J016's delegate (Isha) has no Delta scope |
| J018, J023 | FILED jobs for cancel / reopen / edit tests |
| J024 | Apex job for the client-side user |
| J191–J200 | Orphans: no Checker exists in their scope |
| (access) A026 | Expired temporary Checker assignment (changed responsibility); A019 absent owner |
| (history) | Admin self-escalation attempt (DENIED) in `audit_events.csv` |

## ## Demo

**Recorded walkthrough (about 5 min):** <paste your video link here>

You can also run the same walkthrough yourself. `python demo.py` steps through: access in plain terms → "why can she and he can't?" →
scope denials → maker-checker → absent owner → filed-job cancel with two-person approval → admin self-escalation → the parser →
"waiting for me" → one job's history and "who could have changed it on the 3rd?". To drive the live server instead, run
`python -m nfpl serve` and try, for example:
## Each person's access, in plain terms

_(generated from `data/access_assignments.csv`; the same text is in `data/ACCESS_SUMMARY.md`)_



### Aarav Mehta (U001, employee, active)
- Aarav Mehta has role Maker for every client, every branch, all directions, all modes.
- Aarav Mehta has role Checker for Apex Electronics, every branch, all directions, all modes.
- Aarav Mehta has role Checker for Bharat Pharmaceuticals, every branch, all directions, all modes.

### Priya Sharma (U002, employee, active)
- Priya Sharma has role Checker for Coastal Foods, Mumbai, imports only, AIR only.

### Rohan Desai (U003, employee, active)
- Rohan Desai has role Checker for Delta Automotive, Delhi, imports only, all modes.

### Neha Kapoor (U004, employee, active)
- Neha Kapoor has role Maker for Everest Textiles, Mumbai, all directions, all modes.
- Neha Kapoor has role Checker for Everest Textiles, Mumbai, all directions, all modes. [EXPIRED 2026-09-30]

### Vikram Singh (U005, employee, active)
- Vikram Singh has role Maker for Falcon Machinery, Chennai, exports only, all modes.

### Ananya Rao (U006, employee, active)
- Ananya Rao has role Checker for Global Retail, Chennai, exports only, FCL only.

### Karan Patel (U007, employee, active)
- Karan Patel has role Manager for every client, every branch, all directions, all modes.

### Sneha Iyer (U008, employee, active)
- Sneha Iyer has role Maker for Horizon Chemicals, Kolkata, imports only, LCL only.

### Arjun Nair (U009, employee, active)
- Arjun Nair has role Checker for Horizon Chemicals, Kolkata, imports only, LCL only.
- Arjun Nair has role Checker for Zenith Technologies, Mumbai, exports only, AIR only.

### Meera Joshi (U010, employee, active)
- Meera Joshi has role Maker for Indigo Imports, Bengaluru, imports only, all modes.

### Rahul Verma (U011, employee, active)
- Rahul Verma has role Checker for Indigo Imports, Bengaluru, imports only, all modes.
- Rahul Verma has role Checker for Everest Textiles, Mumbai, all directions, all modes.

### Pooja Shah (U012, employee, active)
- Pooja Shah has role Maker for Zenith Technologies, Mumbai, exports only, AIR only.

### Dev Malhotra (U013, employee, active)
- Dev Malhotra has role Manager for every client, every branch, all directions, all modes.

### Isha Kulkarni (U014, employee, active)
- Isha Kulkarni has role Maker for Apex Electronics, Delhi, imports only, FCL only.

### Sameer Khan (U015, employee, active)
- Sameer Khan has role Checker for Apex Electronics, Delhi, imports only, FCL only.
- Sameer Khan has role Checker for Falcon Machinery, Chennai, exports only, all modes.

### Tanya Bose (U016, employee, active)
- Tanya Bose has role Maker for Bharat Pharmaceuticals, Mumbai, all directions, all modes.

### Aditya Rao (U017, employee, absent)
- Aditya Rao has role Maker for Delta Automotive, Delhi, imports only, all modes.

### Kavya Menon (U018, client_user, active)
- Kavya Menon has role Client User for Apex Electronics, every branch, all directions, all modes.

### Nikhil Gupta (U019, admin, active)
- Nikhil Gupta has role Admin for every client, every branch, all directions, all modes.

### NFPL Overnight Parser (U020, service_account, active)
- NFPL Overnight Parser has role Service Account for every client, every branch, all directions, all modes.
