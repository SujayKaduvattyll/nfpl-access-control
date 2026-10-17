"""Tiny JSON-over-HTTP API (standard library only). Every route is a thin wrapper over Service."""
import json, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from .service import Service
from .engine import ACTIONS

LOCK = threading.Lock()

PAGE = """<!doctype html><meta charset=utf-8><title>NFPL Access Check</title>
<style>
 body{font:15px/1.5 system-ui,Segoe UI,sans-serif;max-width:820px;margin:30px auto;padding:0 16px;color:#1a2b4a}
 h1{font-size:22px} label{display:block;margin:12px 0 4px;font-weight:600} select,input{padding:8px;font-size:15px;width:100%;box-sizing:border-box}
 button{margin:14px 8px 0 0;padding:10px 16px;font-size:15px;border:0;border-radius:6px;background:#1a3a6e;color:#fff;cursor:pointer}
 button.alt{background:#566} #out{margin-top:22px;padding:16px;border-radius:8px;display:none} .ok{background:#e6f6ea;border:1px solid #6c6}
 .no{background:#fdeaea;border:1px solid #d77} .big{font-size:20px;font-weight:700} code{background:#0001;padding:1px 5px;border-radius:4px}
 ul{margin:6px 0 0 18px;padding:0} small{color:#667}
</style>
<h1>Who may do what on which job?</h1>
<label>Person</label><select id=u></select>
<label>Action</label><select id=a></select>
<label>Job id</label><input id=j value=J006 placeholder="e.g. J006">
<button onclick=check()>Check</button><button class=alt onclick=waiting()>Show jobs waiting for this person</button>
<div><label>Compare with another person (optional: "why can she and he can't?")</label><select id=u2><option value="">(none)</option></select></div>
<div id=out></div>
<script>
const $=id=>document.getElementById(id);
fetch('/meta').then(r=>r.json()).then(m=>{
  m.users.forEach(u=>{for(const s of ['u','u2']){const o=document.createElement('option');o.value=u.user_id;o.textContent=u.user_name+' ('+u.user_id+', '+u.user_type+(u.status!='active'?', '+u.status:'')+')';$(s).appendChild(o)}});
  m.actions.forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=a;$('a').appendChild(o)});
  $('u').value='U002';$('a').value='job.check';});
function show(html,ok){const o=$('out');o.style.display='block';o.className=ok===null?'':(ok?'ok':'no');o.innerHTML=html}
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function check(){
  const u=$('u').value,a=$('a').value,j=$('j').value.trim().toUpperCase(),u2=$('u2').value;
  if(u2){const r=await (await fetch(`/explain?user_a=${u}&user_b=${u2}&action=${a}&job_id=${j}`)).json();
    show(`<div class=big>${r.a.allowed?'ALLOWED':'DENIED'} for the first person, ${r.b.allowed?'ALLOWED':'DENIED'} for the second</div><p>${esc(r.summary)}</p>`,null);return}
  const d=await (await fetch(`/authorize?user_id=${u}&action=${a}&job_id=${j}`)).json();
  show(`<div class=big>${d.allowed?'ALLOWED':'DENIED'} <small>(${d.code})</small></div><p>${esc(d.reason)}</p>
   <small>How the decision was reached:</small><ul>${(d.steps||[]).map(s=>'<li>'+esc(s)+'</li>').join('')}</ul>
   ${d.requires_approval?'<p><b>Note:</b> this action would need a second Manager to approve.</p>':''}`,d.allowed)}
async function waiting(){
  const w=await (await fetch(`/users/${$('u').value}/waiting?limit=30`)).json();
  show(`<div class=big>${w.count} job(s) waiting${w.approvals.length?` + ${w.approvals.length} approval(s)`:''}</div><ul>`+
   w.jobs.map(x=>`<li><code>${x.job_id}</code> ${x.stage} &rarr; ${x.actions.join(', ')}${x.attention?' <small>(needs attention: '+x.attention+')</small>':''}</li>`).join('')+'</ul>',null)}
</script>"""


def make_handler(svc):
    GET = [
        (r"^/health$", lambda q: {"status": "ok", "now": svc.now()}),
        (r"^/meta$", lambda q: {"users": [dict(r) for r in svc.db.execute("SELECT user_id,user_name,user_type,status FROM users ORDER BY user_id")],
                               "actions": [a for a in ACTIONS if a != "job.create"]}),
        (r"^/users/(\w+)/access$", lambda q, u: svc.user_access(u)),
        (r"^/users/(\w+)/waiting$", lambda q, u: svc.waiting_for(u, int(q.get("limit", 50)))),
        (r"^/users/(\w+)/jobs$", lambda q, u: svc.visible_jobs(u, q.get("stage"), int(q.get("limit", 50)), int(q.get("offset", 0)))),
        (r"^/jobs/(\w+)$", lambda q, j: svc.job(j)),
        (r"^/jobs/(\w+)/history$", lambda q, j: svc.history(j)),
        (r"^/jobs/(\w+)/who-could-change$", lambda q, j: svc.who_could_change(j, q.get("date", svc.today()))),
        (r"^/authorize$", lambda q: svc.authorize(q.get("user_id", ""), q.get("action", ""), q.get("job_id", "")).to_dict()),
        (r"^/explain$", lambda q: svc.compare(q.get("user_a", ""), q.get("user_b", ""), q.get("action", ""), q.get("job_id", ""))),
    ]
    POST = [
        (r"^/authorize$", lambda b: svc.authorize(b.get("user_id", ""), b.get("action", ""), b.get("job_id", "")).to_dict()),
        (r"^/jobs/(\w+)/actions$", lambda b, j: svc.perform(b.get("actor", ""), b.get("action", ""), j, b.get("reason", ""), b.get("to_user"), b.get("changes"))),
        (r"^/approvals/(\d+)/approve$", lambda b, r: svc.approve(b.get("actor", ""), int(r), True)),
        (r"^/approvals/(\d+)/reject$", lambda b, r: svc.approve(b.get("actor", ""), int(r), False, b.get("reason", ""))),
        (r"^/access/grant$", lambda b: svc.grant_access(b.get("actor", ""), b.get("user_id", ""), b.get("role_id", ""),
                                                          b.get("client_id", "ALL"), b.get("branch_id", "ALL"), b.get("direction", "ALL"),
                                                          b.get("mode", "ALL"), b.get("valid_to", ""), b.get("reason", ""))),
        (r"^/access/(\w+)/revoke$", lambda b, a: svc.revoke_access(b.get("actor", ""), a, b.get("reason", ""))),
        (r"^/users/(\w+)/status$", lambda b, u: svc.set_user_status(b.get("actor", ""), u, b.get("status", ""))),
        (r"^/roles$", lambda b: svc.create_role(b.get("actor", ""), b.get("role_id", ""), b.get("role_name", ""), b.get("permissions", {}), b.get("description", ""))),
        (r"^/parser/jobs$", lambda b: svc.create_job(b.get("actor", ""), b.get("client_id", ""), b.get("branch_id", ""), b.get("direction", ""), b.get("mode", ""), b.get("notes", ""))),
        (r"^/parser/jobs/(\w+)$", lambda b, j: svc.update_intake(b.get("actor", ""), j, b.get("notes", ""))),
        (r"^/admin/expire$", lambda b: svc.expire_assignments()),
    ]

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj, indent=2, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _route(self, table, body):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            for pat, fn in table:
                m = re.match(pat, u.path)
                if m:
                    with LOCK:
                        try:
                            res = fn(body if body is not None else q, *m.groups())
                        except Exception as e:   # fail closed: never leak a stack trace, never default to allow
                            return self._send(500, {"allowed": False, "code": "DENY_UNCERTAIN", "reason": f"Server error: {type(e).__name__}"})
                    if res is None:
                        return self._send(404, {"error": "not found"})
                    code = 200
                    if isinstance(res, dict) and res.get("ok") is False:
                        code = 403
                    return self._send(code, res)
            self._send(404, {"error": "no such route"})

        def do_GET(self):
            if urlparse(self.path).path in ("/", "/ui"):
                data = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._route(GET, None)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            self._route(POST, body)

        def log_message(self, *a):
            pass
    return H


def serve(svc, port=8000, host="127.0.0.1"):
    srv = ThreadingHTTPServer((host, port), make_handler(svc))
    print(f"NFPL access service on http://{host}:{port}  (clock: {svc.now()})", flush=True)
    srv.serve_forever()
