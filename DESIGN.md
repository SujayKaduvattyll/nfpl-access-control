# Design: NFPL Access Control Service

## 1. The model in one page

**Three separate questions, answered in a fixed order.** *Who are you?* (identity: user exists, is active). *What may you see?*
(visibility). *What may you do, to this job, right now?* (authority + stage + guardrails). Keeping visibility and authority
apart is what lets us be strict where it matters (acting) while staying easy day to day (seeing).

**Assignments, not roles.** A person does not "have the Checker role"; they have rows like
*"Priya has role Checker for Coastal Foods, Mumbai, imports only, AIR only."* One row is
`(user, role, client, branch, direction, mode, valid_from, valid_to, status)`, each scope field either a value or `ALL`.
A row matches a job only if all four scope fields match; a person's rows are combined with OR; there are **no deny rules**
(they are hard to explain and easy to get wrong). That one structure expresses every shape the brief asks for:
*Maker everywhere / Checker for a few clients* = one `ALL` row plus one row per client; *Checker for one client, one branch, imports
only* = one narrow row.

**Roles are data.** `role_permissions(role, permission, stages)` maps a role to permission names such as `job.check`
or `job.view` (with an optional list of stages). The engine only knows permission names and the stage machine; it has no `if role == "Checker"`.
A new role is therefore a new row set, created by an Admin through the API, with no deployment.

**One decision function.** `decide(ctx, action, job)` in `engine.py` is pure: it reads a preloaded user context and a job row,
and returns allow/deny, a *reason code*, a plain sentence and a step-by-step trace. Everything else (single checks, the
side-by-side "why can she / can't he", history replays, the "waiting for me" queue) calls it. There is therefore one definition of "allowed" in the system.

**Append-only audit.** Every attempted action, allowed *or denied*, is written with who, what, stage change, the decision, the reason code,
and the assignment that justified it. Assignments are never overwritten, only ended (`valid_to`, `status`), so the past can be replayed.

**The stage machine.** Confirmed → Prepared → Checked → Filed; Cancelled and Reopened as exits and re-entries. Prepare/edit (Maker),
check (Checker, not the preparer), file (Manager, not the preparer). Filed and cancelled jobs are locked.

**The claim and "needs attention".** A job may name a `responsible_user_id`. That is a *claim*: while the owner is active and
still able to do the next step, nobody else may. The service computes a `attention` flag per open job (`NO_ELIGIBLE_ACTOR`,
`OWNER_ABSENT`, `OWNER_OUT_OF_SCOPE`) from who could actually act today. A flag voids the claim and puts the job in front of a Manager.
This one mechanism handles absence, changed responsibilities, orphans and maker-checker dead-ends.

**Fail closed.** Unknown user, action, job, stage, or a malformed assignment (typo in a scope value) is a DENY with a reason code, never a guess.

## 2. Key trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Authority model | scoped assignments + role→permission data | plain roles; or full policy language | Roles alone cannot express "Checker for one client at one branch"; a policy language is more power than 120 staff need and harder to explain. |
| Deny rules | none | explicit deny | Additive grants are easy to audit; guardrails (separation of duties, locked stages) cover the real "must not". |
| Ownership | soft claim that voids itself when unusable | hard assignment | Hard assignment makes absence an outage; a soft claim keeps work moving without silent reassignment. |
| Filing | Manager step | Checker files | A third person on the irreversible step, and it gives Managers a natural queue ("what comes after Maker and Checker"). |
| Evaluation | live, on every request, nothing cached | cache user permissions | Access changes take effect immediately; at this scale a context load is a few indexed rows. |
| Queue count | SQL narrows candidates, `decide()` confirms each | separate counting SQL | Cannot drift from the real rules; a test compares it to brute-force `decide()` for every user. |
| Storage | SQLite | Postgres | Zero-install for the reviewer; the schema and queries are portable. |

## 3. Answers to the 14 questions

1. **Should everyone see everything?** No. Visibility is its own permission (`job.view`), scoped like any other. Makers and Checkers see every stage for jobs in their scope; Client Users see only their client; the parser sees only CONFIRMED jobs; **Managers see from CHECKED onward plus jobs flagged as needing attention**; Admins see no job data. Seeing is wider than acting: a Checker sees a job at every stage but may act only on PREPARED ones, and acting always requires seeing it first.
2. **Maker everywhere, Checker for few; Checker for one client/branch/imports only.** Separate assignment rows, one per grant (see model). Plain-language form: *"Aarav has role Checker for Apex Electronics, every branch, all directions, all modes."* Every denial names the first scope field that did not match ("direction: assignment is IMPORT, job is EXPORT").
3. **Preparer is also qualified to approve.** Separation of duties is checked per job, not per person: the person recorded as `prepared_by` cannot check or file it, whatever roles they hold. The denial says why. If nobody else is qualified (J021), the job is flagged `NO_ELIGIBLE_ACTOR` for a Manager rather than bypassed. Reopening clears `prepared_by`, so the next cycle is judged fresh.
4. **Responsible person absent.** Absent users cannot act, but their assignments are kept for their return. Their jobs flag `OWNER_ABSENT`, which voids the claim, so any qualified person can act and a Manager sees the job. A delegation row records cover, but **a delegation never grants scope**: a delegate must hold the role and scope themselves (the J016 test).
5. **Responsibilities change mid-job.** Revocation or expiry is effective immediately (new actions are refused), but in-flight jobs are not silently moved. The service recomputes flags and the jobs appear as `OWNER_OUT_OF_SCOPE` / `NO_ELIGIBLE_ACTOR` for a Manager, who reassigns them (to someone verified as able to do the next step). Work already done stays attributed in history.
6. **Cancel a filed job / reopen a closed one.** Only Managers, with a written reason, and a **second Manager must approve**; a requester cannot approve their own request; the request is void if the job's stage changed meanwhile. A reopened job restarts the cycle (re-prepare, re-check). Cancelling a not-yet-filed job needs one Manager and a reason. Both steps are in the history.
7. **A client's own employee.** A Client User role with a client-scoped `job.view` and nothing else. Enforced by permissions, not hidden buttons; the role can only be granted to client-side users and client-side users can hold nothing else.
8. **Admin fixing a stuck job / self-escalation.** Admins manage access, not jobs. A stuck job is fixed by a Manager (reassign) after the system flags it. An Admin **cannot change their own access** (grant, revoke or status). Granting Manager, Admin or Service Account to someone else needs a Manager's approval (not the requester, not the grantee). New roles cannot contain `access.manage`. The attempt is audited as DENIED.
9. **The overnight parser.** A Service Account with `job.create` and `job.update_intake`. It creates CONFIRMED jobs with real scope values, may update intake notes only while a job is CONFIRMED, and cannot see jobs past that. It can never prepare, check, file, cancel, reopen or touch access, so it can never be the "maker" a human then "checks". Its actions appear in history as the parser.
10. **"Why can she do this and he can't?"** `GET /explain` returns both decisions side by side with a trace (identity → visibility → authority → stage → guardrail) and a paragraph naming the matched assignment for one and the first failing check for the other.
11. **"Who could have changed this job on the 3rd?"** Because assignments are never overwritten, the service replays the assignments valid on that date against the stages the job passed through that day and lists everyone who could have done any change, separately from who actually did and from denied attempts. Known limit: it ignores personal claims and assumes people were present (stated in the response).
12. **A new role next quarter.** `POST /roles` with a list of existing permission names (and optional stage limits); then an ordinary assignment. A new *permission name* needs engineering; a new combination does not. Admin-only permissions cannot be put in a new role.
13. **"Jobs waiting for me" matches what I can do.** The queue runs SQL to find candidates, then calls the same `decide()` for each; approvals are counted with the same rule. A test checks, for every user, that the queue equals a brute-force `decide()` over all jobs, and that every listed job passes `authorize`.
14. **When the system is unsure.** Deny, give a reason code, and surface the job to a Manager where a human can resolve it. Examples: unknown stage, malformed assignment, no eligible actor. Server errors return a DENY, never an allow.

## 4. Questions we added

- **A. What about jobs nobody can act on?** Computed, not guessed: the `attention` flag finds them (orphans, absent owners, maker-checker dead-ends). They go to Managers' queues.
- **B. Can the requester approve their own request, or can the grantee approve their own grant?** No, in every two-person path.
- **C. Does anything expire on its own?** Yes: `valid_to`. It is enforced by date on every request, so it works even if the daily housekeeping job has not run; flags refresh when it does.
- **D. How does a Manager take over a healthy in-progress job?** Managers do not see healthy CONFIRMED/PREPARED jobs (by design). They mark the owner absent, which flags the job, or wait until it reaches CHECKED.
- **E. Can anyone change their own access?** No, not even an Admin (the single rule that closes self-escalation).

## 5. Known limits

Authentication and caller identity are assumed. Concurrency is a single lock (add an optimistic stage check for multi-process). A Manager's waiting list is ~0.4 s at 200k jobs
(the cost is evaluating each CHECKED job with `decide()`; a materialised queue table would make it constant-time if needed). Full attention recompute after an access
change is ~0.4 s; at larger scale it can be limited to the affected scope. The sample dataset has one Admin, so "second Admin" approval is not used; privileged grants are approved by a Manager instead.
