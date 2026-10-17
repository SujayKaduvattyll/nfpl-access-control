"""Scripted 5-minute walkthrough against the running HTTP service.
   python demo.py            (starts its own server on :8765 with a fresh in-memory copy of the sample data)"""
import json, os, sys, threading, time, urllib.request
from http.server import ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nfpl.service import connect, load_data, Service
from nfpl.server import make_handler

svc = Service(connect(":memory:"), clock="2026-10-17T10:00:00")
load_data(svc.db, os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
srv = ThreadingHTTPServer(("127.0.0.1", 8765), make_handler(svc)); threading.Thread(target=srv.serve_forever, daemon=True).start()
B = "http://127.0.0.1:8765"


def call(method, path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req); return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def step(title): print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
def show(label, d, keys=("allowed", "code", "reason")): print(f"{label}\n   " + "\n   ".join(f"{k}: {d[k]}" for k in keys if k in d))


step("1. Who has what? (plain terms, from the data)")
for u in ("U001", "U002", "U018"):
    for a in call("GET", f"/users/{u}/access")["assignments"]: print("  ", a["plain"])

step("2. 'Why can she do this and he can't?'  Priya vs Aarav checking J006 (Coastal Foods, Mumbai, Import, Air)")
r = call("GET", "/explain?user_a=U002&user_b=U001&action=job.check&job_id=J006"); print(r["summary"])
print("   trace for Priya:", *r["a"]["steps"], sep="\n     - ")

step("3. Scope: Priya on J007 (export) and J008 (Delhi)")
for j in ("J007", "J008"): show(f"Priya check {j}:", call("GET", f"/authorize?user_id=U002&action=job.check&job_id={j}"), ("allowed", "reason"))

step("4. Maker-checker: Aarav prepared J021 and is also an Apex Checker")
d = call("POST", "/jobs/J021/actions", {"actor": "U001", "action": "job.check"})["decision"]; print("  ", d["code"], "-", d["reason"])
print("   J021 is flagged for a Manager:", call("GET", "/jobs/J021")["attention"])

step("5. Absent owner (J022 is held by absent Aditya): Rohan can still check it; Aditya cannot act")
show("Aditya check J022:", call("GET", "/authorize?user_id=U017&action=job.check&job_id=J022"), ("code", "reason"))
show("Rohan check J022:", call("GET", "/authorize?user_id=U003&action=job.check&job_id=J022"), ("allowed", "reason"))

step("6. Filed job J018: edit denied, cancel needs a reason and a SECOND Manager")
show("Aarav edit J018:", call("GET", "/authorize?user_id=U001&action=job.edit&job_id=J018"), ("code", "reason"))
r = call("POST", "/jobs/J018/actions", {"actor": "U007", "action": "job.cancel", "reason": "Client withdrew shipment"}); print("   Karan requests cancel ->", r["status"])
print("   Karan approves own request ->", call("POST", f"/approvals/{r['request_id']}/approve", {"actor": "U007"})["decision"]["code"])
print("   Dev approves ->", call("POST", f"/approvals/{r['request_id']}/approve", {"actor": "U013"})["job"]["stage"])

step("7. Admin cannot make themselves more powerful")
print("  ", call("POST", "/access/grant", {"actor": "U019", "user_id": "U019", "role_id": "R003"})["decision"]["reason"])

step("8. Parser creates a job overnight; it can't prepare, check or file")
j = call("POST", "/parser/jobs", {"actor": "U020", "client_id": "C003", "branch_id": "B001", "direction": "IMPORT", "mode": "AIR"})["job"]; print("   created", j["job_id"], j["stage"], "by", j["created_by"])
print("   parser prepares it ->", call("GET", f"/authorize?user_id=U020&action=job.prepare&job_id={j['job_id']}")["code"])

step("9. 'Jobs waiting for me' equals what I can actually do")
w = call("GET", "/users/U001/waiting?limit=3"); print("   Aarav waiting:", w["count"], "jobs; first:", [(x['job_id'], x['actions']) for x in w["jobs"]])
w = call("GET", "/users/U007/waiting?limit=3"); print("   Karan (Manager) waiting:", w["total"], "items; first:", [(x['job_id'], x['actions']) for x in w["jobs"]])

step("10. A job's history, and 'who could have changed it on the 3rd?'")
for e in call("GET", "/jobs/J021/history"): print(f"   {e['ts']}  {e['actor_name']:<22} {e['action']:<12} {e['decision']:<6} {e['reason'][:60]}")
r = call("GET", "/jobs/J021/who-could-change?date=2026-10-03")
print("   could have changed on 2026-10-03:", [(c["name"], sorted(c["can"])) for c in r["could_have_changed"]])
print("   actually changed / attempted:", [(e["actor_user_id"], e["action"]) for e in r["actually_changed"]], [(e["actor_user_id"], e["action"], "DENIED") for e in r["attempts_denied"]])
srv.shutdown()
