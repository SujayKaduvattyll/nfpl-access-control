import csv, json, os, sqlite3, datetime as dt
from collections import defaultdict
from . import engine as E
from .engine import DIMS, STAGES, FINAL, OPEN_STAGES, ACTIONS, NEXT_ACTION, decide, decide_global, Ctx

HERE = os.path.dirname(__file__)
PRIVILEGED_ROLES = {"R003", "R005", "R006"}     # granting these needs a second person
JOB_COLS = ["job_id", "client_id", "branch_id", "direction", "mode", "stage", "responsible_user_id", "created_by",
            "prepared_by", "checked_by", "filed_by", "created_at", "updated_at", "scenario", "notes", "attention"]


def connect(path=":memory:"):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(HERE, "schema.sql")) as f:
        conn.executescript(f.read())
    return conn


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_data(conn, data_dir):
    """One command: wipe and load every CSV, then compute attention flags."""
    for t in ["branches", "clients", "users", "roles", "role_permissions", "access_assignments", "delegations",
              "jobs", "audit_events", "approval_requests"]:
        conn.execute(f"DELETE FROM {t}")
    def ins(table, cols, rows):
        conn.executemany(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                         ([r.get(c, "") for c in cols] for r in rows))
    p = lambda n: os.path.join(data_dir, n)
    ins("branches", ["branch_id", "branch_name"], _rows(p("branches.csv")))
    ins("clients", ["client_id", "client_name"], _rows(p("clients.csv")))
    ins("users", ["user_id", "user_name", "user_type", "status"], _rows(p("users.csv")))
    ins("roles", ["role_id", "role_name", "description"], _rows(p("roles.csv")))
    ins("role_permissions", ["role_id", "permission", "stages"], _rows(p("role_permissions.csv")))
    ins("access_assignments", ["access_id", "user_id", "role_id", "client_id", "branch_id", "direction", "mode",
                               "valid_from", "valid_to", "status", "notes"], _rows(p("access_assignments.csv")))
    if os.path.exists(p("delegations.csv")):
        ins("delegations", ["delegation_id", "from_user_id", "to_user_id", "valid_from", "valid_to", "created_by", "notes"],
            _rows(p("delegations.csv")))
    ins("jobs", JOB_COLS[:-1], _rows(p("jobs.csv")))
    ev = _rows(p("audit_events.csv"))
    ins("audit_events", ["event_id", "ts", "job_id", "actor_user_id", "action", "from_stage", "to_stage", "decision",
                         "reason", "approver_user_id"], ev)
    conn.execute("UPDATE audit_events SET reason_code = CASE decision WHEN 'ALLOW' THEN 'ALLOW' ELSE 'DENY' END")
    conn.commit()
    Service(conn).refresh_attention()
    conn.commit()


class Service:
    def __init__(self, conn, clock=None):
        self.db = conn
        self._clock = clock
        self._perms = None
        self._names = None

    # ------------------------------------------------------------------ basics
    def now(self):
        if self._clock:
            return self._clock() if callable(self._clock) else self._clock
        return os.environ.get("NFPL_NOW") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def today(self):
        return self.now()[:10]

    def names(self):
        if self._names is None:
            n = {"users": {}, "clients": {}, "branches": {}, "roles": {}}
            for t, k, c in [("users", "user_id", "user_name"), ("clients", "client_id", "client_name"),
                            ("branches", "branch_id", "branch_name"), ("roles", "role_id", "role_name")]:
                n[t] = {r[0]: r[1] for r in self.db.execute(f"SELECT {k},{c} FROM {t}")}
            self._names = n
        return self._names

    def role_perms(self):
        if self._perms is None:
            m = defaultdict(dict)
            for r in self.db.execute("SELECT role_id,permission,stages FROM role_permissions"):
                m[r["role_id"]][r["permission"]] = None if r["stages"] in ("*", "") else set(r["stages"].split("|"))
            self._perms = m
        return self._perms

    def invalidate(self):
        self._perms = self._names = None

    def job(self, job_id):
        r = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------------ context
    def _valid_scope(self, a):
        n = self.names()
        return (a["client_id"] == "ALL" or a["client_id"] in n["clients"]) and \
               (a["branch_id"] == "ALL" or a["branch_id"] in n["branches"]) and \
               (a["direction"] == "ALL" or a["direction"] in E.DIRECTIONS) and \
               (a["mode"] == "ALL" or a["mode"] in E.MODES)

    def ctx(self, user_id, as_of=None):
        """Build the user's context from the database right now (no caching => changes apply immediately)."""
        u = self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        day = as_of or self.today()
        live, bad = [], []
        for r in self.db.execute("SELECT * FROM access_assignments WHERE user_id=?", (user_id,)):
            a = dict(r)
            in_window = (not a["valid_from"] or a["valid_from"] <= day) and (not a["valid_to"] or a["valid_to"] >= day)
            if as_of is None and a["status"] != "ACTIVE":
                continue
            if not in_window:
                continue
            perms = self.role_perms().get(a["role_id"])
            if perms is None or not self._valid_scope(a):
                bad.append(a["access_id"])
                continue
            a["perms"] = perms
            live.append(a)
        deleg = {r[0] for r in self.db.execute(
            "SELECT from_user_id FROM delegations WHERE to_user_id=? AND valid_from<=? AND valid_to>=?", (user_id, day, day))}
        return Ctx(user_id, dict(u) if u else None, live, self.names(), bad, deleg, as_of)

    # ------------------------------------------------------------------ audit
    def audit(self, job_id, actor, action, fr, to, decision, reason, code="", approver="", assignment=""):
        cur = self.db.execute(
            "INSERT INTO audit_events (event_id,ts,job_id,actor_user_id,action,from_stage,to_stage,decision,reason,"
            "approver_user_id,reason_code,assignment_id) VALUES ('',?,?,?,?,?,?,?,?,?,?,?)",
            (self.now(), job_id or "", actor, action, fr or "", to or "", decision, reason, approver, code, assignment))
        self.db.execute("UPDATE audit_events SET event_id=printf('E%05d',seq) WHERE seq=?", (cur.lastrowid,))

    # ------------------------------------------------------------------ attention flags
    def refresh_attention(self, job_ids=None):
        """Flag open jobs a Manager needs to look at. Computed (not guessed) from who could act today:
           NO_ELIGIBLE_ACTOR  nobody active in scope could do the next step (incl. maker-checker separation)
           OWNER_ABSENT       the person holding the job is not active
           OWNER_OUT_OF_SCOPE the person holding the job can no longer do the next step
           A flag voids the owner's personal claim, so other qualified people may pick the job up."""
        active = {r[0] for r in self.db.execute("SELECT user_id FROM users WHERE status='active'")}
        today = self.today()
        by_action = defaultdict(lambda: defaultdict(list))   # action -> client -> [assignments]
        for r in self.db.execute("SELECT * FROM access_assignments WHERE status='ACTIVE'"):
            a = dict(r)
            if r["user_id"] not in active or (a["valid_from"] and a["valid_from"] > today) or (a["valid_to"] and a["valid_to"] < today):
                continue
            perms = self.role_perms().get(a["role_id"], {})
            if not self._valid_scope(a):
                continue
            for act in set(NEXT_ACTION.values()):
                if act in perms:
                    by_action[act][a["client_id"]].append(a)
        cache = {}
        def eligible(job):
            key = (job["client_id"], job["branch_id"], job["direction"], job["mode"], job["stage"])
            if key not in cache:
                act = NEXT_ACTION[job["stage"]]
                s = set()
                for c in (job["client_id"], "ALL"):
                    for a in by_action[act].get(c, ()):
                        if not E.scope_mismatch(a, job):
                            st = self.role_perms()[a["role_id"]][act]
                            if E._stage_ok(st, job["stage"]):
                                s.add(a["user_id"])
                cache[key] = s
            return cache[key]
        q = "SELECT job_id,client_id,branch_id,direction,mode,stage,responsible_user_id,prepared_by,attention FROM jobs WHERE stage NOT IN ('FILED','CANCELLED')"
        params = []
        if job_ids is not None:
            job_ids = list(job_ids)
            if not job_ids:
                return 0
            q += f" AND job_id IN ({','.join('?' * len(job_ids))})"
            params = job_ids
        changes = []
        for r in self.db.execute(q, params):
            j = dict(r)
            el = eligible(j)
            owner = j["responsible_user_id"]
            sod = j["stage"] in ("PREPARED", "CHECKED")
            pool = el - {j["prepared_by"]} if sod else el
            if not pool:
                flag = "NO_ELIGIBLE_ACTOR"
            elif owner and owner not in active:
                flag = "OWNER_ABSENT"
            elif owner and owner not in el:
                flag = "OWNER_OUT_OF_SCOPE"
            else:
                flag = ""
            if flag != (j["attention"] or ""):
                changes.append((flag, j["job_id"]))
        self.db.executemany("UPDATE jobs SET attention=? WHERE job_id=?", changes)
        if job_ids is not None and not changes:
            return 0
        return len(changes)

    # ------------------------------------------------------------------ queries: decisions
    def authorize(self, user_id, action, job_id):
        """Dry-run decision (not written to the audit log)."""
        return decide(self.ctx(user_id), action, self.job(job_id))

    def compare(self, user_a, user_b, action, job_id):
        da, dbb = self.authorize(user_a, action, job_id), self.authorize(user_b, action, job_id)
        n = self.names()["users"]
        if da.allowed == dbb.allowed:
            summary = f"Both {'can' if da.allowed else 'cannot'} do this."
        else:
            yes, no = (da, dbb) if da.allowed else (dbb, da)
            summary = (f"{n.get(yes.user_id, yes.user_id)} can because: {yes.reason}  "
                       f"{n.get(no.user_id, no.user_id)} cannot because: {no.reason}")
        return {"action": action, "job_id": job_id, "summary": summary, "a": da.to_dict(), "b": dbb.to_dict()}

    def user_access(self, user_id):
        c = self.ctx(user_id)
        if c.user is None:
            return None
        rows = []
        for r in self.db.execute("SELECT * FROM access_assignments WHERE user_id=? ORDER BY access_id", (user_id,)):
            a = dict(r)
            a["plain"] = E.describe_assignment(c, a) + ("" if a["status"] == "ACTIVE" else f" [{a['status']}, ended {a['valid_to']}]")
            rows.append(a)
        return {"user": c.user, "assignments": rows, "malformed_ignored": c.malformed}

    # ------------------------------------------------------------------ queries: lists
    @staticmethod
    def _scope_clause(a):
        cl, p = [], []
        for d in DIMS:
            if a[d] != "ALL":
                cl.append(f"{d}=?")
                p.append(a[d])
        return ("(" + " AND ".join(cl) + ")" if cl else "1=1"), p

    def _visible_where(self, ctx):
        parts, params = [], []
        for a in ctx.assignments:
            sc, sp = self._scope_clause(a)
            if "job.view" in a["perms"]:
                st = a["perms"]["job.view"]
                if st is None:
                    parts.append(sc); params += sp
                else:
                    parts.append(f"({sc} AND stage IN ({','.join('?' * len(st))}))"); params += sp + sorted(st)
            if "job.view_attention" in a["perms"]:
                parts.append(f"({sc} AND attention!='' AND stage NOT IN ('FILED','CANCELLED'))"); params += sp
        return (" OR ".join(parts) if parts else "0"), params

    def visible_jobs(self, user_id, stage=None, limit=50, offset=0):
        c = self.ctx(user_id)
        if c.user is None or c.user["status"] != "active":
            return {"total": 0, "jobs": []}
        w, p = self._visible_where(c)
        extra, ep = ("", [])
        if stage:
            extra, ep = " AND stage=?", [stage]
        total = self.db.execute(f"SELECT COUNT(*) FROM jobs WHERE ({w}){extra}", p + ep).fetchone()[0]
        rows = self.db.execute(f"SELECT * FROM jobs WHERE ({w}){extra} ORDER BY job_id LIMIT ? OFFSET ?", p + ep + [limit, offset]).fetchall()
        return {"total": total, "jobs": [dict(r) for r in rows]}

    def waiting_for(self, user_id, limit=50):
        """'Jobs waiting for me'. SQL narrows the candidates; the SAME decide() then confirms each one,
        so the count can never promise something the user cannot do."""
        c = self.ctx(user_id)
        if c.user is None or c.user["status"] != "active":
            return {"count": 0, "jobs": [], "approvals": [], "total": 0}
        found = {}
        for action in ("job.prepare", "job.check", "job.file", "job.reassign"):
            spec = ACTIONS[action]
            stages = [s for s in (spec["frm"]) if s in STAGES]
            parts, params = [], []
            for a in c.assignments:
                if action not in a["perms"]:
                    continue
                sc, sp = self._scope_clause(a)
                parts.append(sc); params += sp
            if not parts:
                continue
            where = f"({' OR '.join(parts)}) AND stage IN ({','.join('?' * len(stages))})"
            params += stages
            if action == "job.reassign":
                where += " AND attention!=''"
            elif spec["claim"]:
                mine = ["", user_id] + sorted(c.delegated_from)
                where += f" AND (responsible_user_id IN ({','.join('?' * len(mine))}) OR attention!='')"
                params += mine
            for r in self.db.execute(f"SELECT * FROM jobs WHERE {where}", params):
                j = dict(r)
                if NEXT_ACTION.get(j["stage"]) != action and action != "job.reassign":
                    continue
                if action == "job.reassign" and not E.is_attention(j):
                    continue
                d = decide(c, action, j)
                if d.allowed:
                    found.setdefault(j["job_id"], {"job": j, "actions": []})["actions"].append(action)
        items = sorted(found.values(), key=lambda x: x["job"]["job_id"])
        approvals = self.pending_approvals_for(user_id, c)
        return {"count": len(items), "approvals": approvals, "total": len(items) + len(approvals),
                "jobs": [{"job_id": i["job"]["job_id"], "stage": i["job"]["stage"], "actions": i["actions"],
                          "attention": i["job"]["attention"]} for i in items[:limit]]}

    # ------------------------------------------------------------------ history
    def history(self, job_id):
        rows = self.db.execute("SELECT * FROM audit_events WHERE job_id=? ORDER BY ts, seq", (job_id,)).fetchall()
        n = self.names()["users"]
        out = []
        for r in rows:
            e = dict(r)
            e["actor_name"] = n.get(e["actor_user_id"], e["actor_user_id"])
            out.append(e)
        return out

    def stage_at(self, job_id, ts_exclusive):
        j = self.job(job_id)
        stage, created = None, j["created_at"] if j else None
        for r in self.db.execute("SELECT * FROM audit_events WHERE job_id=? AND decision='ALLOW' AND ts<? ORDER BY ts,seq", (job_id, ts_exclusive)):
            if r["to_stage"]:
                stage = r["to_stage"]
        return stage

    def who_could_change(self, job_id, date):
        """Who COULD have changed the job on `date` (by the assignments valid that day), and who actually did."""
        j = self.job(job_id)
        if j is None:
            return None
        nxt = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        evs = [dict(r) for r in self.db.execute(
            "SELECT * FROM audit_events WHERE job_id=? AND ts>=? AND ts<? ORDER BY ts,seq", (job_id, date, nxt))]
        start = self.stage_at(job_id, date)
        stages = []
        if start:
            stages.append(start)
        for e in evs:
            if e["decision"] == "ALLOW" and e["to_stage"] and e["to_stage"] not in stages:
                stages.append(e["to_stage"])
        prep = ""
        for r in self.db.execute("SELECT actor_user_id FROM audit_events WHERE job_id=? AND action='job.prepare' AND decision='ALLOW' AND ts<? ORDER BY ts DESC,seq DESC LIMIT 1", (job_id, nxt)):
            prep = r[0]
        change_actions = [a for a in ACTIONS if a not in ("job.view", "job.create")]
        users = [r[0] for r in self.db.execute("SELECT DISTINCT user_id FROM access_assignments")]
        could = []
        for u in users:
            c = self.ctx(u, as_of=date)
            hits = {}
            for st in stages:
                pj = dict(j, stage=st, prepared_by=prep or j["prepared_by"], attention="")
                for act in change_actions:
                    d = decide(c, act, pj, ignore_claim=True)
                    if d.allowed:
                        hits.setdefault(act, {"stages": [], "assignment_id": d.assignment_id})["stages"].append(st)
            if hits:
                could.append({"user_id": u, "name": self.names()["users"].get(u, u), "can": hits})
        return {"job_id": job_id, "date": date, "stages_that_day": stages,
                "note": "Evaluated from the assignments valid on that date. Personal claims and each user's status that day are not "
                        "reconstructed (so an owner's claim is ignored and everyone with a valid assignment is listed).",
                "could_have_changed": could,
                "actually_changed": [e for e in evs if e["decision"] == "ALLOW" and e["action"] != "job.view"],
                "attempts_denied": [e for e in evs if e["decision"] != "ALLOW"]}

    # ------------------------------------------------------------------ actions
    def perform(self, actor, action, job_id, reason="", to_user=None, changes=None):
        """Single entry point for every job action. Always audited, allowed or not."""
        job = self.job(job_id)
        c = self.ctx(actor)
        d = decide(c, action, job)
        if d.allowed and action in ("job.cancel", "job.reopen") and not (reason or "").strip():
            d.allowed, d.code, d.reason = False, "DENY_REASON_REQUIRED", f"'{action}' requires a written reason."
        if d.allowed and action == "job.reassign":
            d = self._check_reassign_target(d, job, to_user)
        if not d.allowed:
            self.audit(job_id, actor, action, job and job["stage"], "", "DENY", d.reason, d.code)
            self.db.commit()
            return {"ok": False, "decision": d.to_dict()}
        spec = ACTIONS[action]
        if d.requires_approval:
            cur = self.db.execute("INSERT INTO approval_requests (action,job_id,stage_at_request,requester,reason,payload,status,created_ts) "
                                  "VALUES (?,?,?,?,?,?,?,?)", (action, job_id, job["stage"], actor, reason, json.dumps({}), "PENDING", self.now()))
            self.audit(job_id, actor, action + ".request", job["stage"], "", "ALLOW",
                       f"Second-Manager approval requested: {reason}", "PENDING_APPROVAL", assignment=d.assignment_id)
            self.db.commit()
            return {"ok": True, "status": "PENDING_APPROVAL", "request_id": cur.lastrowid, "decision": d.to_dict()}
        self._execute(actor, action, job, reason, d, to_user=to_user, changes=changes)
        self.db.commit()
        return {"ok": True, "status": "DONE", "decision": d.to_dict(), "job": self.job(job_id)}

    def _check_reassign_target(self, d, job, to_user):
        if not to_user:
            d.allowed, d.code, d.reason = False, "DENY_INVALID_REQUEST", "Reassignment needs a 'to_user'."
            return d
        tc = self.ctx(to_user)
        nxt = NEXT_ACTION.get(job["stage"])
        td = decide(tc, nxt, job, ignore_claim=True) if nxt else None
        if td is None or not td.allowed:
            d.allowed, d.code = False, "DENY_TARGET_NOT_QUALIFIED"
            d.reason = f"{to_user} cannot do the next step ({nxt}) on this job: {td.reason if td else 'no next step'}"
        return d

    def _execute(self, actor, action, job, reason, d, approver="", to_user=None, changes=None):
        spec, now = ACTIONS[action], self.now()
        fr, to = job["stage"], spec["to"]
        sets = {"updated_at": now}
        if to:
            sets["stage"] = to
            sets["responsible_user_id"] = ""            # progression releases the claim back to the next pool
        if action == "job.prepare":
            sets.update(prepared_by=actor, checked_by="", filed_by="")
        elif action == "job.check":
            sets["checked_by"] = actor
        elif action == "job.file":
            sets["filed_by"] = actor
        elif action == "job.reopen":
            sets.update(prepared_by="", checked_by="", filed_by="")
        elif action == "job.reassign":
            sets["responsible_user_id"] = to_user
        if action in ("job.edit", "job.update_intake") and changes and "notes" in changes:
            sets["notes"] = str(changes["notes"])[:500]
        cols = list(sets)
        self.db.execute(f"UPDATE jobs SET {', '.join(k + '=?' for k in cols)} WHERE job_id=?",
                        [sets[k] for k in cols] + [job["job_id"]])
        self.audit(job["job_id"], actor, action, fr, to or fr, "ALLOW", (reason + " | " if reason else "") + d.reason,
                   "ALLOW", approver=approver, assignment=d.assignment_id)
        self.refresh_attention([job["job_id"]])

    def pending_approvals_for(self, user_id, c=None):
        c = c or self.ctx(user_id)
        out = []
        for r in self.db.execute("SELECT * FROM approval_requests WHERE status='PENDING' ORDER BY request_id"):
            if r["requester"] == user_id:
                continue
            if r["action"] in ("job.cancel", "job.reopen"):
                j = self.job(r["job_id"])
                if j and j["stage"] == r["stage_at_request"] and decide(c, r["action"], j).allowed:
                    out.append({"request_id": r["request_id"], "action": r["action"], "job_id": r["job_id"], "requested_by": r["requester"], "reason": r["reason"]})
            elif r["action"] == "access.grant":
                p = json.loads(r["payload"])
                if decide_global(c, "access.approve").allowed and user_id not in (r["requester"], p.get("user_id")):
                    out.append({"request_id": r["request_id"], "action": r["action"], "job_id": "", "requested_by": r["requester"], "reason": r["reason"]})
        return out

    def approve(self, approver, request_id, approve=True, note=""):
        r = self.db.execute("SELECT * FROM approval_requests WHERE request_id=?", (request_id,)).fetchone()
        if r is None or r["status"] != "PENDING":
            return {"ok": False, "decision": {"allowed": False, "code": "DENY_INVALID_REQUEST", "reason": "No such pending request."}}
        r = dict(r)
        if r["requester"] == approver:
            self.audit(r["job_id"], approver, r["action"] + ".approve", "", "", "DENY", "Requester cannot approve their own request.", "DENY_SELF_APPROVAL")
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "code": "DENY_SELF_APPROVAL", "reason": "You requested this, so a different person must approve it."}}
        c = self.ctx(approver)
        if not approve:
            self.db.execute("UPDATE approval_requests SET status='REJECTED',decided_by=?,decided_ts=? WHERE request_id=?", (approver, self.now(), request_id))
            self.audit(r["job_id"], approver, r["action"] + ".reject", "", "", "ALLOW", note or "Rejected", "REJECTED", approver=approver)
            self.db.commit()
            return {"ok": True, "status": "REJECTED"}
        if r["action"] in ("job.cancel", "job.reopen"):
            job = self.job(r["job_id"])
            if job["stage"] != r["stage_at_request"]:
                return {"ok": False, "decision": {"allowed": False, "code": "DENY_STALE_REQUEST", "reason": "The job changed since this was requested; ask again."}}
            d = decide(c, r["action"], job)
            if not d.allowed:
                self.audit(r["job_id"], approver, r["action"] + ".approve", job["stage"], "", "DENY", d.reason, d.code)
                self.db.commit()
                return {"ok": False, "decision": d.to_dict()}
            self.db.execute("UPDATE approval_requests SET status='APPROVED',decided_by=?,decided_ts=? WHERE request_id=?", (approver, self.now(), request_id))
            self._execute(r["requester"], r["action"], job, r["reason"], d, approver=approver)
            self.db.commit()
            return {"ok": True, "status": "DONE", "job": self.job(r["job_id"])}
        if r["action"] == "access.grant":
            p = json.loads(r["payload"])
            g = decide_global(c, "access.approve")
            if not g.allowed or approver in (r["requester"], p["user_id"]):
                self.audit("", approver, "access.grant.approve", "", "", "DENY", g.reason if not g.allowed else "Approver cannot be the requester or the grantee.", "DENY_NOT_APPROVER")
                self.db.commit()
                return {"ok": False, "decision": {"allowed": False, "code": "DENY_NOT_APPROVER", "reason": g.reason if not g.allowed else "Approver cannot be the requester or the grantee."}}
            self.db.execute("UPDATE approval_requests SET status='APPROVED',decided_by=?,decided_ts=? WHERE request_id=?", (approver, self.now(), request_id))
            aid = self._insert_assignment(p, r["requester"], approver)
            self.db.commit()
            return {"ok": True, "status": "DONE", "access_id": aid}
        return {"ok": False, "decision": {"allowed": False, "code": "DENY_UNCERTAIN", "reason": "Unknown request type."}}

    # ------------------------------------------------------------------ access management
    def _next_access_id(self):
        m = self.db.execute("SELECT MAX(CAST(SUBSTR(access_id,2) AS INTEGER)) FROM access_assignments").fetchone()[0] or 0
        return f"A{m + 1:03d}"

    def _insert_assignment(self, p, actor, approver=""):
        aid = self._next_access_id()
        self.db.execute("INSERT INTO access_assignments (access_id,user_id,role_id,client_id,branch_id,direction,mode,valid_from,valid_to,status,notes) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (aid, p["user_id"], p["role_id"], p["client_id"], p["branch_id"], p["direction"], p["mode"],
                         self.today(), p.get("valid_to") or "", "ACTIVE", p.get("reason", "")))
        self.audit("", actor, "access.grant", "", "", "ALLOW", f"{aid}: {p['role_id']} for {p['user_id']} "
                   f"({p['client_id']}/{p['branch_id']}/{p['direction']}/{p['mode']}). {p.get('reason', '')}", "ALLOW", approver=approver)
        self.refresh_attention()
        return aid

    def grant_access(self, actor, user_id, role_id, client_id="ALL", branch_id="ALL", direction="ALL", mode="ALL", valid_to="", reason=""):
        c = self.ctx(actor)
        g = decide_global(c, "access.manage")
        def deny(code, why):
            self.audit("", actor, "access.grant", "", "", "DENY", why, code)
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "code": code, "reason": why}}
        if not g.allowed:
            return deny(g.code, g.reason)
        if user_id == actor:
            return deny("DENY_SELF_GRANT", "Nobody may change their own access, including Admins. Another Admin must do it.")
        p = dict(user_id=user_id, role_id=role_id, client_id=client_id, branch_id=branch_id, direction=direction, mode=mode, valid_to=valid_to, reason=reason)
        probe = dict(p, access_id="?")
        if self.db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is None or role_id not in self.role_perms() or not self._valid_scope(probe):
            return deny("DENY_INVALID_REQUEST", "Unknown user, role or scope value.")
        grantee = self.db.execute("SELECT user_type FROM users WHERE user_id=?", (user_id,)).fetchone()[0]
        if (role_id == "R004") != (grantee == "client_user"):
            return deny("DENY_INVALID_REQUEST", "Client User role is only for client-side users, and client-side users may only hold it.")
        if role_id in PRIVILEGED_ROLES:
            cur = self.db.execute("INSERT INTO approval_requests (action,job_id,stage_at_request,requester,reason,payload,status,created_ts) VALUES ('access.grant','','',?,?,?,'PENDING',?)",
                                  (actor, reason, json.dumps(p), self.now()))
            self.audit("", actor, "access.grant.request", "", "", "ALLOW", f"Privileged grant of {role_id} to {user_id} needs a Manager's approval. {reason}", "PENDING_APPROVAL")
            self.db.commit()
            return {"ok": True, "status": "PENDING_APPROVAL", "request_id": cur.lastrowid}
        aid = self._insert_assignment(p, actor)
        self.db.commit()
        return {"ok": True, "status": "DONE", "access_id": aid}

    def revoke_access(self, actor, access_id, reason=""):
        c = self.ctx(actor)
        g = decide_global(c, "access.manage")
        row = self.db.execute("SELECT * FROM access_assignments WHERE access_id=?", (access_id,)).fetchone()
        def deny(code, why):
            self.audit("", actor, "access.revoke", "", "", "DENY", why, code)
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "code": code, "reason": why}}
        if not g.allowed:
            return deny(g.code, g.reason)
        if row is None or row["status"] != "ACTIVE":
            return deny("DENY_INVALID_REQUEST", "No such active assignment.")
        if row["user_id"] == actor:
            return deny("DENY_SELF_GRANT", "Nobody may change their own access, including Admins.")
        self.db.execute("UPDATE access_assignments SET status='REVOKED', valid_to=? WHERE access_id=?", (self.today(), access_id))
        self.audit("", actor, "access.revoke", "", "", "ALLOW", f"{access_id} revoked for {row['user_id']}. {reason}", "ALLOW")
        flagged = self.refresh_attention()
        self.db.commit()
        return {"ok": True, "status": "DONE", "jobs_flagged_for_manager": flagged}

    def expire_assignments(self):
        """Mark assignments whose valid_to has passed as EXPIRED and flag affected in-flight jobs."""
        rows = self.db.execute("SELECT access_id FROM access_assignments WHERE status='ACTIVE' AND valid_to!='' AND valid_to<?", (self.today(),)).fetchall()
        for r in rows:
            self.db.execute("UPDATE access_assignments SET status='EXPIRED' WHERE access_id=?", (r[0],))
            self.audit("", "SYSTEM", "access.expire", "", "", "ALLOW", f"{r[0]} reached its end date", "ALLOW")
        flagged = self.refresh_attention()
        self.db.commit()
        return {"expired": [r[0] for r in rows], "jobs_flagged": flagged}

    def set_user_status(self, actor, user_id, status):
        c = self.ctx(actor)
        g = decide_global(c, "user.set_status")
        if not g.allowed or status not in ("active", "absent", "inactive"):
            why = g.reason if not g.allowed else "Status must be active, absent or inactive."
            self.audit("", actor, "user.set_status", "", "", "DENY", why, g.code if not g.allowed else "DENY_INVALID_REQUEST")
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "code": g.code, "reason": why}}
        if user_id == actor:
            self.audit("", actor, "user.set_status", "", "", "DENY", "Cannot change your own status.", "DENY_SELF_GRANT")
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "code": "DENY_SELF_GRANT", "reason": "Cannot change your own status."}}
        self.db.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
        self.audit("", actor, "user.set_status", "", "", "ALLOW", f"{user_id} -> {status}", "ALLOW")
        flagged = self.refresh_attention()
        self.db.commit()
        return {"ok": True, "jobs_flagged_for_manager": flagged}

    def create_role(self, actor, role_id, role_name, permissions, description=""):
        """New roles are data: a list of existing permission names. No engineering needed."""
        c = self.ctx(actor)
        g = decide_global(c, "role.manage")
        why = None
        if not g.allowed:
            why = g.reason
        elif not permissions or any(p not in E.ALL_PERMISSIONS for p in permissions):
            why = "Unknown permission name; new permission names need an engineering change."
        elif {"access.manage", "role.manage", "access.approve"} & set(permissions):
            why = "Admin-only permissions cannot be placed in a new role."
        elif role_id in self.role_perms():
            why = "Role already exists."
        if why:
            self.audit("", actor, "role.create", "", "", "DENY", why, g.code if not g.allowed else "DENY_INVALID_REQUEST")
            self.db.commit()
            return {"ok": False, "decision": {"allowed": False, "reason": why}}
        self.db.execute("INSERT INTO roles VALUES (?,?,?)", (role_id, role_name, description))
        for perm, st in permissions.items():
            self.db.execute("INSERT INTO role_permissions VALUES (?,?,?)", (role_id, perm, st or "*"))
        self.audit("", actor, "role.create", "", "", "ALLOW", f"{role_id} {role_name}: {sorted(permissions)}", "ALLOW")
        self.invalidate()
        self.db.commit()
        return {"ok": True}

    # ------------------------------------------------------------------ parser (service account)
    def create_job(self, actor, client_id, branch_id, direction, mode, notes=""):
        c = self.ctx(actor)
        pseudo = dict(job_id="(new)", client_id=client_id, branch_id=branch_id, direction=direction, mode=mode, stage="NEW",
                      responsible_user_id="", prepared_by="", attention="")
        d = decide(c, "job.create", pseudo)
        if d.allowed and not self._valid_scope({"client_id": client_id, "branch_id": branch_id, "direction": direction, "mode": mode}) or \
           (d.allowed and "ALL" in (client_id, branch_id, direction, mode)):
            d.allowed, d.code, d.reason = False, "DENY_INVALID_REQUEST", "A job needs a real client, branch, direction and mode."
        if not d.allowed:
            self.audit("", actor, "job.create", "", "", "DENY", d.reason, d.code)
            self.db.commit()
            return {"ok": False, "decision": d.to_dict()}
        m = self.db.execute("SELECT MAX(CAST(SUBSTR(job_id,2) AS INTEGER)) FROM jobs").fetchone()[0] or 0
        jid = f"J{m + 1:03d}"
        now = self.now()
        self.db.execute("INSERT INTO jobs (job_id,client_id,branch_id,direction,mode,stage,responsible_user_id,created_by,prepared_by,checked_by,filed_by,created_at,updated_at,scenario,notes,attention) "
                        "VALUES (?,?,?,?,?,'CONFIRMED','',?,'','','',?,?,'',?,'')", (jid, client_id, branch_id, direction, mode, actor, now, now, notes))
        self.audit(jid, actor, "job.create", "", "CONFIRMED", "ALLOW", d.reason, "ALLOW", assignment=d.assignment_id)
        self.refresh_attention([jid])
        self.db.commit()
        return {"ok": True, "job": self.job(jid)}

    def update_intake(self, actor, job_id, notes):
        return self.perform(actor, "job.update_intake", job_id, changes={"notes": notes})
