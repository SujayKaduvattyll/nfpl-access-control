"""
The authorization engine.

decide(ctx, action, job) is a PURE function: it looks only at the preloaded user context and the job row,
never at the database, so the same function can be reused for single checks, "why can she / can't he",
history replays and the 'jobs waiting for me' queue (which is what keeps those answers consistent).

Order of checks (first failure wins, and the reason says which one):
  1. identity     user exists and is active                       (absent users cannot act)
  2. visibility   some live assignment lets the user SEE the job  (acting always requires seeing)
  3. authority    a live assignment whose role grants the action, whose scope matches the job
  4. stage        the job is at a stage where the action is valid
  5. guardrails   separation of duties; claim held by an active, still-qualified owner
Anything unclear fails closed (DENY_UNCERTAIN).
"""
from dataclasses import dataclass, field, asdict

DIMS = ("client_id", "branch_id", "direction", "mode")
DIM_LABEL = {"client_id": "client", "branch_id": "branch", "direction": "direction", "mode": "mode"}
DIRECTIONS = {"IMPORT", "EXPORT"}
MODES = {"AIR", "FCL", "LCL"}
STAGES = ["CONFIRMED", "PREPARED", "CHECKED", "FILED", "CANCELLED", "REOPENED"]
FINAL = {"FILED", "CANCELLED"}
OPEN_STAGES = [s for s in STAGES if s not in FINAL]

# action -> rules.  frm: stages the action is valid from.  claim: a personal owner's claim is respected.
# sod: actor must not be the person who prepared the job.  two_person: stages needing a second approver.
ACTIONS = {
    "job.view":          dict(frm=None, to=None, claim=False, sod=False),
    "job.edit":          dict(frm=("CONFIRMED", "REOPENED"), to=None, claim=True, sod=False),
    "job.prepare":       dict(frm=("CONFIRMED", "REOPENED"), to="PREPARED", claim=True, sod=False),
    "job.check":         dict(frm=("PREPARED",), to="CHECKED", claim=True, sod=True),
    "job.file":          dict(frm=("CHECKED",), to="FILED", claim=False, sod=True),
    "job.cancel":        dict(frm=("CONFIRMED", "PREPARED", "CHECKED", "REOPENED", "FILED"), to="CANCELLED",
                              claim=False, sod=False, two_person=("FILED",)),
    "job.reopen":        dict(frm=("FILED", "CANCELLED"), to="REOPENED", claim=False, sod=False,
                              two_person=("FILED", "CANCELLED")),
    "job.reassign":      dict(frm=tuple(OPEN_STAGES), to=None, claim=False, sod=False),
    "job.create":        dict(frm=("NEW",), to="CONFIRMED", claim=False, sod=False, no_view=True),
    "job.update_intake": dict(frm=("CONFIRMED",), to=None, claim=False, sod=False),
}
# Non-job permissions (global; checked with decide_global)
GLOBAL_PERMS = {"access.manage", "access.approve", "user.set_status", "role.manage"}
ALL_PERMISSIONS = set(ACTIONS) | GLOBAL_PERMS | {"job.view_attention"}
# The next step for a job at each stage (used by the queue, reassignment and the attention flags)
NEXT_ACTION = {"CONFIRMED": "job.prepare", "REOPENED": "job.prepare", "PREPARED": "job.check", "CHECKED": "job.file"}


@dataclass
class Decision:
    allowed: bool
    code: str
    reason: str
    action: str = ""
    user_id: str = ""
    job_id: str = ""
    assignment_id: str = ""
    role: str = ""
    requires_approval: bool = False
    steps: list = field(default_factory=list)   # the trace that explains how we got here

    def to_dict(self):
        return asdict(self)


@dataclass
class Ctx:
    """Everything decide() needs to know about one user at one moment."""
    user_id: str
    user: dict | None
    assignments: list          # live, well-formed assignments, each with a 'perms' {perm: set(stages)|None}
    names: dict                # {'users':{}, 'clients':{}, 'branches':{}, 'roles':{}}
    malformed: list = field(default_factory=list)
    delegated_from: set = field(default_factory=set)
    as_of: str | None = None   # set for historical replays: skips the user's *current* status


def nm(ctx, kind, key):
    if key == "ALL":
        return "all"
    return ctx.names.get(kind, {}).get(key, key)


def scope_mismatch(a, job):
    return [(d, a[d], job.get(d)) for d in DIMS if a[d] != "ALL" and a[d] != job.get(d)]


def describe_scope(ctx, a):
    c = "every client" if a["client_id"] == "ALL" else nm(ctx, "clients", a["client_id"])
    b = "every branch" if a["branch_id"] == "ALL" else nm(ctx, "branches", a["branch_id"])
    d = "all directions" if a["direction"] == "ALL" else a["direction"].lower() + "s only"
    m = "all modes" if a["mode"] == "ALL" else a["mode"] + " only"
    return f"{c}, {b}, {d}, {m}"


def describe_assignment(ctx, a):
    return (f"{nm(ctx, 'users', ctx.user_id)} has role {nm(ctx, 'roles', a['role_id'])} for "
            f"{describe_scope(ctx, a)}")


def _stage_ok(perm_stages, stage):
    return perm_stages is None or stage in perm_stages


def is_attention(job):
    return bool(job.get("attention")) and job.get("stage") not in FINAL


def _deny(d, code, reason, *steps):
    d.allowed, d.code, d.reason = False, code, reason
    d.steps.extend(steps)
    return d


def _mismatch_text(ctx, mism):
    return "; ".join(f"{DIM_LABEL[dim]}: assignment is {nm(ctx, {'client_id': 'clients', 'branch_id': 'branches'}.get(dim, dim), want)}, "
                     f"job is {nm(ctx, {'client_id': 'clients', 'branch_id': 'branches'}.get(dim, dim), got)}"
                     for dim, want, got in mism)


def _view_via(ctx, job):
    """Return (assignment, how) if the user may see this job, else None."""
    for a in ctx.assignments:
        if "job.view" in a["perms"] and not scope_mismatch(a, job) and _stage_ok(a["perms"]["job.view"], job["stage"]):
            return a, "role"
    if is_attention(job):
        for a in ctx.assignments:
            if "job.view_attention" in a["perms"] and not scope_mismatch(a, job):
                return a, "attention"
    return None


def decide(ctx, action, job, ignore_claim=False):
    """Fail-closed wrapper: any surprise becomes a DENY with a reason code."""
    d = Decision(False, "DENY_UNCERTAIN", "", action=action, user_id=ctx.user_id, job_id=(job or {}).get("job_id", ""))
    try:
        return _decide(ctx, action, job, ignore_claim, d)
    except Exception as e:  # pragma: no cover - defensive
        return _deny(d, "DENY_UNCERTAIN", f"The system could not decide safely ({type(e).__name__}); denied by default.")


def _decide(ctx, action, job, ignore_claim, d):
    who = nm(ctx, "users", ctx.user_id)
    if ctx.user is None:
        return _deny(d, "DENY_UNKNOWN_USER", f"User {ctx.user_id} does not exist.")
    if action not in ACTIONS:
        return _deny(d, "DENY_UNKNOWN_ACTION", f"'{action}' is not a known action.")
    if job is None:
        return _deny(d, "DENY_UNKNOWN_JOB", "That job does not exist.")
    spec = ACTIONS[action]
    stage = job.get("stage")
    if stage not in STAGES and stage != "NEW":
        return _deny(d, "DENY_UNCERTAIN", f"Job has an unrecognised stage '{stage}'; denied by default.")
    if ctx.as_of is None and ctx.user["status"] != "active":
        return _deny(d, "DENY_USER_NOT_ACTIVE",
                     f"{who} is {ctx.user['status']}, so cannot act or see jobs. Their assignments are kept and "
                     f"their jobs stay available to other qualified people.", "identity: not active")
    d.steps.append(f"identity: {who} ({ctx.user['user_type']}) is active")

    # ---- 2. visibility (acting always requires being able to see the job) ----
    if not spec.get("no_view") or action == "job.view":
        v = _view_via(ctx, job)
        if v is None:
            if not any("job.view" in a["perms"] for a in ctx.assignments):
                return _deny(d, "DENY_NO_ROLE", f"{who} holds no role that gives access to job data.", "visibility: no viewing role")
            hint = ""
            if any("job.view_attention" in a["perms"] for a in ctx.assignments):
                hint = " Managers see jobs from CHECKED onward, plus jobs flagged as needing attention."
            closest = [a for a in ctx.assignments if "job.view" in a["perms"]]
            best = min(closest, key=lambda a: len(scope_mismatch(a, job)))
            mism = scope_mismatch(best, job)
            if mism:
                return _deny(d, "DENY_SCOPE_MISMATCH",
                             f"{who} cannot see this job{'' if action == 'job.view' else ' (so cannot ' + action + ' it)'}: {_mismatch_text(ctx, mism)}.", f"visibility: closest assignment {best['access_id']}")
            return _deny(d, "DENY_NOT_VISIBLE",
                         f"{who} cannot see jobs at stage {stage} with this role.{hint}", "visibility: stage not visible to role")
        a, how = v
        d.steps.append(f"visibility: {a['access_id']} lets {who} see this job ({how})")
        if action == "job.view":
            d.allowed, d.code, d.assignment_id, d.role = True, "ALLOW", a["access_id"], a["role_id"]
            d.reason = (f"{describe_assignment(ctx, a)}; this job matches"
                        + (" (shown because it needs attention)" if how == "attention" else "") + ".")
            return d

    # ---- 3. authority: role + scope ----
    candidates, had_perm, closest = [], False, None
    for a in ctx.assignments:
        if action not in a["perms"]:
            continue
        had_perm = True
        mism = scope_mismatch(a, job)
        if mism:
            if closest is None or len(mism) < len(closest[1]):
                closest = (a, mism)
            continue
        candidates.append(a)
    if not candidates:
        if not had_perm:
            if ctx.malformed:
                return _deny(d, "DENY_UNCERTAIN",
                             f"{who} has assignment(s) {', '.join(ctx.malformed)} that are malformed and were ignored; "
                             f"denied by default until an Admin fixes them.", "authority: malformed assignments ignored")
            roles = sorted({nm(ctx, 'roles', a['role_id']) for a in ctx.assignments}) or ["no role"]
            return _deny(d, "DENY_NO_ROLE", f"{who} ({', '.join(roles)}) has no role that grants '{action}'.", "authority: no role grants this action")
        a, mism = closest
        return _deny(d, "DENY_SCOPE_MISMATCH",
                     f"{who} is a {nm(ctx, 'roles', a['role_id'])} for {describe_scope(ctx, a)}, but this job does not match: "
                     f"{_mismatch_text(ctx, mism)}.", f"authority: closest assignment {a['access_id']}")
    a = candidates[0]
    d.assignment_id, d.role = a["access_id"], a["role_id"]
    d.steps.append(f"authority: {a['access_id']} grants '{action}' and its scope matches the job")

    # ---- 4. stage ----
    if spec["frm"] and stage not in spec["frm"]:
        extra = ""
        if stage in FINAL and action in ("job.edit", "job.prepare", "job.check"):
            extra = " Filed and cancelled jobs are locked; a Manager must reopen them (two-person approval)."
        return _deny(d, "DENY_STAGE", f"'{action}' only applies to {'/'.join(spec['frm'])} jobs; this job is {stage}.{extra}",
                     "stage: not valid at this stage")
    if not _stage_ok(a["perms"][action], stage):
        return _deny(d, "DENY_STAGE", f"Role {nm(ctx, 'roles', a['role_id'])} cannot use '{action}' on {stage} jobs.", "stage: role restriction")
    d.steps.append(f"stage: {stage} allows '{action}'")

    # ---- 5. guardrails ----
    if spec.get("sod") and job.get("prepared_by") == ctx.user_id:
        return _deny(d, "DENY_SEPARATION_OF_DUTIES",
                     f"{who} prepared this job, so cannot also {action.split('.')[1]} it (maker-checker separation). "
                     f"Another qualified person is needed.", "guardrail: separation of duties")
    if spec["claim"] and not ignore_claim:
        owner = job.get("responsible_user_id") or ""
        claim_live = owner and owner != ctx.user_id and not job.get("attention") and owner not in ctx.delegated_from
        if claim_live:
            return _deny(d, "DENY_OWNED_BY_OTHER",
                         f"This job is held by {nm(ctx, 'users', owner)}, who is active and still qualified. "
                         f"A Manager can reassign it.", "guardrail: claim held by another active owner")
        if owner and owner != ctx.user_id:
            d.steps.append("guardrail: owner's claim is void (absent, out of scope or delegated) so any qualified person may act")
    d.allowed, d.code = True, "ALLOW"
    if spec.get("two_person") and stage in spec["two_person"]:
        d.requires_approval = True
        d.reason = (f"{describe_assignment(ctx, a)}. '{action}' on a {stage} job needs a written reason and approval "
                    f"from a second Manager; this request will be queued for them.")
    else:
        d.reason = f"{describe_assignment(ctx, a)}; the job matches and is {stage}."
    return d


def decide_global(ctx, perm):
    """Non-job permissions (access.manage, access.approve, ...). Scope is irrelevant."""
    d = Decision(False, "DENY_NO_ROLE", "", action=perm, user_id=ctx.user_id)
    who = nm(ctx, "users", ctx.user_id)
    if ctx.user is None:
        return _deny(d, "DENY_UNKNOWN_USER", f"User {ctx.user_id} does not exist.")
    if ctx.user["status"] != "active":
        return _deny(d, "DENY_USER_NOT_ACTIVE", f"{who} is {ctx.user['status']}.")
    for a in ctx.assignments:
        if perm in a["perms"]:
            d.allowed, d.code, d.assignment_id, d.role = True, "ALLOW", a["access_id"], a["role_id"]
            d.reason = f"{who} holds role {nm(ctx, 'roles', a['role_id'])} which grants '{perm}'."
            return d
    return _deny(d, "DENY_NO_ROLE", f"{who} holds no role that grants '{perm}'.")
