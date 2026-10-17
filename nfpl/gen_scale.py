"""Generate the scale case (default 500 users, 200,000 jobs) in the same CSV format as data/, and benchmark it."""
import csv, os, random, time, datetime as dt, shutil
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIRS, MODES = ["IMPORT", "EXPORT"], ["AIR", "FCL", "LCL"]


def generate(out, n_users=500, n_jobs=200000, seed=7):
    rnd = random.Random(seed)
    os.makedirs(out, exist_ok=True)
    for f in ("branches.csv", "roles.csv", "role_permissions.csv"):
        shutil.copy(os.path.join(ROOT, "data", f), out)
    clients = [f"C{i:03d}" for i in range(1, 81)]
    branches = [f"B{i:03d}" for i in range(1, 6)]

    def writer(name, hdr):
        w = csv.writer(open(os.path.join(out, name), "w", newline="")); w.writerow(hdr); return w
    w = writer("clients.csv", ["client_id", "client_name"])
    for c in clients: w.writerow([c, f"Client {c}"])
    uw = writer("users.csv", ["user_id", "user_name", "user_type", "status"])
    aw = writer("access_assignments.csv", ["access_id", "user_id", "role_id", "client_id", "branch_id", "direction", "mode", "valid_from", "valid_to", "status", "notes"])
    users, aid = [], [0]
    def assign(u, role, c="ALL", b="ALL", d="ALL", m="ALL", note=""):
        aid[0] += 1; aw.writerow([f"A{aid[0]:05d}", u, role, c, b, d, m, "2026-01-01", "", "ACTIVE", note])
    n_client_users, n_mgr, n_admin = 30, 15, 3
    n_emp = n_users - n_client_users - n_mgr - n_admin - 1
    makers, checkers, mgrs = [], [], []
    k = 0
    def newuser(t, status="active"):
        nonlocal k
        k += 1; u = f"U{k:04d}"; uw.writerow([u, f"User {k}", t, status]); return u
    for i in range(n_emp):
        u = newuser("employee", "absent" if rnd.random() < 0.03 else "active")
        role = "R001" if i % 2 == 0 else "R002"
        (makers if role == "R001" else checkers).append(u)
        for _ in range(rnd.randint(1, 3)):
            assign(u, role, rnd.choice(clients), rnd.choice(branches + ["ALL"]), rnd.choice(DIRS + ["ALL"]), rnd.choice(MODES + ["ALL"]))
    for _ in range(n_mgr): mgrs.append(newuser("employee")); assign(mgrs[-1], "R003")
    for _ in range(n_admin): assign(newuser("admin"), "R005")
    for _ in range(n_client_users): assign(newuser("client_user"), "R004", rnd.choice(clients))
    assign(newuser("service_account"), "R006")
    # jobs: pick a (client, branch, direction, mode) some maker AND checker cover, so most jobs are workable
    import csv as _c
    rows = list(_c.DictReader(open(os.path.join(out, "access_assignments.csv"))))
    cover = defaultdict(lambda: {"R001": [], "R002": []})
    for r in rows:
        if r["role_id"] in ("R001", "R002"):
            for b in (branches if r["branch_id"] == "ALL" else [r["branch_id"]]):
                for d in (DIRS if r["direction"] == "ALL" else [r["direction"]]):
                    for m in (MODES if r["mode"] == "ALL" else [r["mode"]]):
                        cover[(r["client_id"], b, d, m)][r["role_id"]].append(r["user_id"])
    combos = [c for c, v in cover.items() if v["R001"] and v["R002"]]
    jw = writer("jobs.csv", ["job_id", "client_id", "branch_id", "direction", "mode", "stage", "responsible_user_id", "created_by", "prepared_by", "checked_by", "filed_by", "created_at", "updated_at", "scenario", "notes"])
    ew = writer("audit_events.csv", ["event_id", "ts", "job_id", "actor_user_id", "action", "from_stage", "to_stage", "decision", "reason", "approver_user_id"])
    parser = f"U{k:04d}"
    base, ev = dt.datetime(2026, 1, 1), 0
    stages = ["CONFIRMED"] * 20 + ["PREPARED"] * 25 + ["CHECKED"] * 15 + ["FILED"] * 36 + ["CANCELLED"] * 2 + ["REOPENED"] * 2
    for i in range(1, n_jobs + 1):
        c, b, d, m = rnd.choice(combos)
        mk, ck = rnd.choice(cover[(c, b, d, m)]["R001"]), rnd.choice(cover[(c, b, d, m)]["R002"])
        if ck == mk: ck = rnd.choice(cover[(c, b, d, m)]["R002"])
        st, mg = rnd.choice(stages), rnd.choice(mgrs)
        resp = {"CONFIRMED": mk, "PREPARED": "", "CHECKED": "", "FILED": "", "CANCELLED": "", "REOPENED": mk}[st]
        prep = "" if st == "CONFIRMED" else mk
        chk = ck if st in ("CHECKED", "FILED", "CANCELLED") else ""
        fil = mg if st == "FILED" else ""
        ts = (base + dt.timedelta(minutes=rnd.randint(0, 290 * 24 * 60))).isoformat(timespec="minutes")
        jid = f"J{i:06d}"
        jw.writerow([jid, c, b, d, m, st, resp, parser, prep, chk, fil, ts, ts, "", ""])
        ev += 1; ew.writerow([f"E{ev:07d}", ts, jid, parser, "job.create", "", "CONFIRMED", "ALLOW", "Created by overnight parser", ""])
        for s, a, who in (("PREPARED", "job.prepare", prep), ("CHECKED", "job.check", chk), ("FILED", "job.file", fil)):
            if st in ("PREPARED", "CHECKED", "FILED") and ["PREPARED", "CHECKED", "FILED"].index(s) <= ["PREPARED", "CHECKED", "FILED"].index(st):
                ev += 1; ew.writerow([f"E{ev:07d}", ts, jid, who, a, "", s, "ALLOW", "Scope and stage matched", ""])
    open(os.path.join(out, "delegations.csv"), "w").write("delegation_id,from_user_id,to_user_id,valid_from,valid_to,created_by,notes\n")
    print(f"generated {n_users} users, {n_jobs} jobs, {aid[0]} assignments -> {out}/")


def bench(data):
    from .service import connect, load_data, Service
    t = time.time(); conn = connect(":memory:"); load_data(conn, data)
    print(f"load + attention flags: {time.time() - t:.1f}s")
    svc = Service(conn, clock="2026-10-17T10:00:00")
    users = [r[0] for r in conn.execute("SELECT user_id FROM users WHERE user_type='employee' LIMIT 400")]
    mgr = [r[0] for r in conn.execute("SELECT user_id FROM access_assignments WHERE role_id='R003' LIMIT 1")][0]
    broad = [r[0] for r in conn.execute("SELECT user_id FROM access_assignments WHERE role_id='R001' GROUP BY user_id ORDER BY COUNT(*) DESC LIMIT 1")][0]
    def timeit(label, fn, n=1):
        t = time.time(); r = None
        for _ in range(n): r = fn()
        print(f"{label:<58}{(time.time() - t) / n * 1000:9.1f} ms"); return r
    timeit("visible jobs page (manager, 50 rows + count)", lambda: svc.visible_jobs(mgr, limit=50))
    timeit("visible jobs page (typical maker)", lambda: svc.visible_jobs(users[0], limit=50), 20)
    t = time.time(); [svc.waiting_for(u) for u in users[:50]]
    print(f"{'waiting-for-me, average per user over 50 users':<58}{(time.time() - t) / 50 * 1000:9.1f} ms")
    timeit("waiting-for-me (manager: CHECKED to file + attention)", lambda: svc.waiting_for(mgr), 1)
    jid = conn.execute("SELECT job_id FROM jobs WHERE stage='PREPARED' LIMIT 1").fetchone()[0]
    timeit("single authorize() decision", lambda: svc.authorize(users[1], "job.check", jid), 200)
    timeit("who-could-change replay for one job (500 users)", lambda: svc.who_could_change(jid, "2026-06-01"), 1)
    timeit("refresh_attention (full recompute, after any access change)", lambda: svc.refresh_attention(), 1)
    n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"jobs: {n}  users: {conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]}")
