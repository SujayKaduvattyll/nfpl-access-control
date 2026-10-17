import argparse, os, sys
from .service import connect, load_data, Service
from .server import serve
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_user(svc, text):
    """Accept a user id (U002) or part of a name (priya)."""
    rows = svc.db.execute("SELECT user_id,user_name FROM users").fetchall()
    t = (text or "").strip().lower()
    exact = [r for r in rows if r[0].lower() == t]
    hits = exact or [r for r in rows if t and t in r[1].lower()]
    return hits[0][0] if len(hits) == 1 else None, hits


def ask(a):
    from .engine import ACTIONS
    if not os.path.exists(a.db):
        sys.exit("No database yet. Run:  py -m nfpl load")
    svc = Service(connect(a.db))
    while True:
        interactive = a.user is None
        user = a.user or input("Person (name or id, e.g. priya or U002): ")
        uid, hits = resolve_user(svc, user)
        if uid is None:
            print("  No single match for", repr(user), "->", [h[1] for h in hits] or "nobody with that name"); 
        else:
            action = a.action or input(f"Action ({', '.join(x for x in ACTIONS if x != 'job.create')}): ").strip()
            job = a.job or input("Job id (e.g. J006): ").strip().upper()
            d = svc.authorize(uid, action, job)
            print("\n" + ("ALLOWED" if d.allowed else "DENIED"), f"({d.code})")
            print("Why:", d.reason)
            for s in d.steps:
                print("   -", s)
            print()
        if not interactive:
            break
        if input("Ask another? [Y/n] ").strip().lower() == "n":
            break
        a.user = a.action = a.job = None


def main():
    p = argparse.ArgumentParser(prog="python -m nfpl")
    sub = p.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("load", help="create the database and load the CSVs")
    l.add_argument("--data", default=os.path.join(ROOT, "data")); l.add_argument("--db", default="nfpl.db")
    s = sub.add_parser("serve", help="run the API")
    s.add_argument("--db", default="nfpl.db"); s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    s.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 when hosting online")
    s.add_argument("--load", action="store_true", help="(re)load the sample data first (handy on a host with no database yet)")
    g = sub.add_parser("gen-scale", help="generate the 500-user / 200,000-job case as CSVs")
    g.add_argument("--out", default="data_scale"); g.add_argument("--users", type=int, default=500); g.add_argument("--jobs", type=int, default=200000)
    q = sub.add_parser("ask", help="ask: may <person> do <action> on <job>?  e.g.  ask priya job.check J006")
    q.add_argument("user", nargs="?"); q.add_argument("action", nargs="?"); q.add_argument("job", nargs="?")
    q.add_argument("--db", default="nfpl.db")
    b = sub.add_parser("bench", help="load a scale dataset and time the hot paths")
    b.add_argument("--data", default="data_scale")
    a = p.parse_args()
    if a.cmd == "load":
        if os.path.exists(a.db): os.remove(a.db)
        c = connect(a.db); load_data(c, a.data)
        n = {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in ("users", "access_assignments", "jobs", "audit_events")}
        print("loaded", n, "->", a.db)
    elif a.cmd == "ask":
        ask(a)
    elif a.cmd == "serve":
        c = connect(a.db)
        if a.load or c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0:
            load_data(c, os.path.join(ROOT, "data")); print("sample data loaded")
        serve(Service(c), a.port, a.host)
    elif a.cmd == "gen-scale":
        from .gen_scale import generate; generate(a.out, a.users, a.jobs)
    elif a.cmd == "bench":
        from .gen_scale import bench; bench(a.data)

main()
