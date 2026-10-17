import csv, random, datetime as dt, collections
random.seed(42)
R=lambda f:list(csv.DictReader(open(f)))
old_jobs=R('/tmp/ds/jobs.csv'); old_assign=R('/tmp/ds/access_assignments.csv')
users={u['user_id']:u for u in R('users.csv')}
for a in old_assign: a.update(valid_from='2026-01-01',valid_to='',status='ACTIVE')
extra=[
 ('A023','U009','R002','C010','B001','EXPORT','AIR','Arjun also checks Zenith air exports at Mumbai'),
 ('A024','U011','R002','C005','B001','ALL','ALL','Rahul also checks Everest Textiles at Mumbai'),
 ('A025','U015','R002','C006','B003','EXPORT','ALL','Sameer also checks Falcon exports at Chennai'),
 ('A026','U004','R002','C005','B001','ALL','ALL','EXPIRED: Neha was a temporary Checker for Everest (changed responsibility test)'),
]
for (aid,u,r,c,b,d,m,n) in extra:
    old_assign.append(dict(access_id=aid,user_id=u,role_id=r,client_id=c,branch_id=b,direction=d,mode=m,notes=n,
        valid_from='2026-01-01',valid_to='2026-09-30' if aid=='A026' else '',status='EXPIRED' if aid=='A026' else 'ACTIVE'))
F=['access_id','user_id','role_id','client_id','branch_id','direction','mode','valid_from','valid_to','status','notes']
w=csv.DictWriter(open('access_assignments.csv','w',newline=''),F);w.writeheader()
for a in old_assign:w.writerow({k:a[k] for k in F})
def match(a,j): return all(a[k] in('ALL',j[k]) for k in('client_id','branch_id','direction','mode'))
def holders(role,j):
    return [a['user_id'] for a in old_assign if a['role_id']==role and a['status']=='ACTIVE' and match(a,j) and users[a['user_id']]['status']=='active']
MG=['U007','U013']
clients=[f'C{i:03d}' for i in range(1,11)];branches=[f'B{i:03d}' for i in range(1,6)]
combos=[dict(client_id=c,branch_id=b,direction=d,mode=m) for c in clients for b in branches for d in('IMPORT','EXPORT') for m in('AIR','FCL','LCL')]
covered=[x for x in combos if holders('R002',x) and len(set(holders('R001',x)))>=2]
uncovered=[x for x in combos if not holders('R002',x)]
base=dt.datetime(2026,9,1,9,0)
jobs=[];events=[];eid=[0]
def ev(ts,job,actor,action,fr,to,dec='ALLOW',reason='',appr=''):
    eid[0]+=1;events.append(dict(event_id=f'E{eid[0]:05d}',ts=ts.isoformat(timespec='minutes'),job_id=job,actor_user_id=actor,action=action,from_stage=fr or '',to_stage=to or '',decision=dec,reason=reason,approver_user_id=appr))
def mk(jid,c,b,d,m,stage,resp,prep='',chk='',filed='',scen='',note=''):
    jobs.append(dict(job_id=jid,client_id=c,branch_id=b,direction=d,mode=m,stage=stage,responsible_user_id=resp,created_by='U020',
        prepared_by=prep,checked_by=chk,filed_by=filed,created_at='',updated_at='',scenario=scen,notes=note))
spec={
'J002':('U001','','','','Prepared by Aarav'),
'J003':('U014','','','',''),'J004':('U014','','','',''),'J005':('U001','','','',''),
'J006':('U001','','','priya_allow','Priya (Checker C003/B001/IMPORT/AIR): ALLOW'),
'J007':('U001','','','priya_deny_direction','Priya is import-only: DENY on export'),
'J008':('U001','','','priya_deny_branch','Priya is Mumbai-only: DENY on Delhi'),
'J009':('U017','','','',''),'J010':('U017','','','',''),
'J011':('U008','','','',''),'J012':('U008','U009','','',''),'J013':('U010','','','',''),'J014':('U010','','','',''),
'J015':('U012','','','',''),'J016':('U017','','','absent_owner','Owner U017 absent; delegate U014 lacks Delta scope'),
'J017':('U001','U001','U007','historic_self_check','Aarav both prepared and checked: breach recorded in history'),
'J018':('U001','U001','U013','filed_job_cancel_reopen','Filed Apex job for cancel/reopen tests'),
'J020':('U005','','','',''),
'J021':('U001','','','maker_checker_conflict','Aarav prepared and is also an Apex Checker: cannot check'),
'J022':('U017','','','absent_owner','Owner U017 absent; backup Maker U016 has no Delta scope'),
'J023':('U017','U003','U007','filed_job','Filed Delta job; try cancel/reopen/edit'),
'J024':('U001','U011','','client_user_view','Apex job: Kavya (U018) may view only'),
}
for j in old_jobs[:24]:
    mk(j['job_id'],j['client_id'],j['branch_id'],j['direction'],j['mode'],j['stage'],j['responsible_user_id'],*spec.get(j['job_id'],('','','','','')))
def pick_stage():
    r=random.random()
    return 'CONFIRMED' if r<.22 else 'PREPARED' if r<.50 else 'CHECKED' if r<.70 else 'FILED' if r<.92 else 'CANCELLED' if r<.96 else 'REOPENED'
orph=iter(random.sample(uncovered,8))
while len(jobs)<200:
    jid=f'J{len(jobs)+1:03d}'
    if len(jobs)>=190:
        x=next(orph,None) or random.choice(uncovered); stage=random.choice(['PREPARED','PREPARED','CONFIRMED']); scen='orphan_no_checker'
    else: x=random.choice(covered); stage=pick_stage(); scen=''
    narrow=[u for u in holders('R001',x) if u not in('U001','U017')]
    maker=random.choice(narrow or ['U001'])
    chk_pool=[u for u in holders('R002',x) if u!=maker]
    chk=random.choice(chk_pool) if chk_pool else ''
    mgr=random.choice(MG)
    c,b,d,m=x['client_id'],x['branch_id'],x['direction'],x['mode']
    if scen:
        if stage=='CONFIRMED': mk(jid,c,b,d,m,stage,maker,scen=scen,note='No Checker exists in this scope')
        else: mk(jid,c,b,d,m,stage,'',prep=maker,scen=scen,note='No Checker in scope; surfaces in Manager needs-assignment list')
    elif stage=='CONFIRMED': mk(jid,c,b,d,m,stage,maker)
    elif not chk: mk(jid,c,b,d,m,'CONFIRMED',maker)
    elif stage=='PREPARED': mk(jid,c,b,d,m,stage,chk,prep=maker)
    elif stage=='CHECKED': mk(jid,c,b,d,m,stage,mgr,prep=maker,chk=chk)
    elif stage=='FILED': mk(jid,c,b,d,m,stage,mgr,prep=maker,chk=chk,filed=mgr)
    elif stage=='CANCELLED': mk(jid,c,b,d,m,stage,mgr,prep=maker,chk=chk,note='Cancelled with reason; approved by second Manager')
    else: mk(jid,c,b,d,m,stage,maker,prep=maker,chk=chk,filed=mgr,note='Reopened from FILED; must be re-prepared and re-checked')
order=['CONFIRMED','PREPARED','CHECKED','FILED']
act={'PREPARED':'job.prepare','CHECKED':'job.check','FILED':'job.file'}
for j in jobs:
    t=base+dt.timedelta(days=random.randint(0,30),hours=random.randint(0,6),minutes=random.randint(0,59))
    FIX=j['job_id'] in('J007','J021','J023','J024')
    if FIX: t=dt.datetime(2026,10,3,9,10)
    j['created_at']=t.isoformat(timespec='minutes')
    ev(t,j['job_id'],'U020','job.create',None,'CONFIRMED',reason='Created by overnight parser')
    st=j['stage']
    if st in order: path=order[1:order.index(st)+1]
    elif st=='REOPENED': path=order[1:]
    else: path=order[1:2] if j['prepared_by'] else []
    actor={'PREPARED':j['prepared_by'],'CHECKED':j['checked_by'],'FILED':j['filed_by']}
    prev='CONFIRMED'
    for s in path:
        t+=dt.timedelta(hours=1 if FIX else random.randint(2,20))
        ev(t,j['job_id'],actor[s],act[s],prev,s,reason='Scope and stage matched'); prev=s
    if st=='CANCELLED': t+=dt.timedelta(hours=5);ev(t,j['job_id'],'U007','job.cancel',prev,'CANCELLED',reason='Client withdrew shipment',appr='U013')
    if st=='REOPENED': t+=dt.timedelta(days=2);ev(t,j['job_id'],'U007','job.reopen','FILED','REOPENED',reason='Correction requested by client',appr='U013')
    j['updated_at']=t.isoformat(timespec='minutes')
D=dt.datetime
ev(D(2026,10,3,11,0),'J021','U001','job.check','PREPARED','CHECKED','DENY','Maker-checker conflict: actor prepared this job')
ev(D(2026,10,3,11,5),'J007','U002','job.check','PREPARED','CHECKED','DENY','Checker scope is IMPORT only; job is EXPORT')
ev(D(2026,10,3,13,0),'J023','U013','job.reopen','FILED','REOPENED','DENY','Reopening a filed job needs a second Manager approval; none given')
ev(D(2026,10,3,13,0),'J024','U018','job.edit','CHECKED','CHECKED','DENY','Client User role is view-only')
for a in old_assign: ev(D(2026,1,1,9,0),'','U019','access.grant','','',reason=f"{a['access_id']}: {a['role_id']} for {a['user_id']}",appr='U013' if a['user_id']=='U019' else '')
ev(D(2026,9,30,18,0),'','U019','access.expire','','',reason='A026 expired: U004 temporary Checker for C005')
ev(D(2026,10,5,10,0),'','U019','access.grant','','','DENY','Self-escalation: admin tried to grant own role; blocked, needs a second Admin')
F2=['event_id','ts','job_id','actor_user_id','action','from_stage','to_stage','decision','reason','approver_user_id']
events.sort(key=lambda e:e['ts'])
w=csv.DictWriter(open('audit_events.csv','w',newline=''),F2);w.writeheader();[w.writerow(e) for e in events]
F3=['job_id','client_id','branch_id','direction','mode','stage','responsible_user_id','created_by','prepared_by','checked_by','filed_by','created_at','updated_at','scenario','notes']
w=csv.DictWriter(open('jobs.csv','w',newline=''),F3);w.writeheader();[w.writerow(j) for j in jobs]
w=csv.writer(open('delegations.csv','w',newline=''));w.writerow(['delegation_id','from_user_id','to_user_id','valid_from','valid_to','created_by','notes'])
w.writerow(['DG001','U017','U014','2026-10-01','2026-10-31','U007','Cover for absent Aditya. U014 has no Delta scope, so this grants no authority'])
nm=lambda i:users[i]['user_name']; cn={c['client_id']:c['client_name'] for c in R('clients.csv')}; bn={b['branch_id']:b['branch_name'] for b in R('branches.csv')}; rn={r['role_id']:r['role_name'] for r in R('roles.csv')}
lines=['# Who has what access (plain terms)\n']
for uid in sorted(users):
    lines.append(f"**{nm(uid)}** ({uid}, {users[uid]['user_type']}, {users[uid]['status']})")
    for a in [a for a in old_assign if a['user_id']==uid]:
        c='every client' if a['client_id']=='ALL' else cn[a['client_id']]; b='every branch' if a['branch_id']=='ALL' else bn[a['branch_id']]
        d='all directions' if a['direction']=='ALL' else a['direction'].lower()+'s only'; m='all modes' if a['mode']=='ALL' else a['mode']+' only'
        tag=' [EXPIRED '+a['valid_to']+']' if a['status']=='EXPIRED' else ''
        lines.append(f"- {nm(uid)} has role {rn[a['role_id']]} for {c}, {b}, {d}, {m}.{tag}")
    lines.append('')
open('ACCESS_SUMMARY.md','w').write('\n'.join(lines))
print(len(jobs),dict(collections.Counter(j['stage'] for j in jobs)),len(events))
print('PREPARED w/o any checker in scope:',sum(1 for j in jobs if j['stage']=='PREPARED' and not holders('R002',j)))
print('PREPARED w/ responsible checker lacking scope:',sum(1 for j in jobs if j['stage']=='PREPARED' and j['responsible_user_id'] and j['responsible_user_id'] not in holders('R002',j)))
print('CONFIRMED w/ responsible lacking maker scope:',sum(1 for j in jobs if j['stage']=='CONFIRMED' and j['responsible_user_id'] not in holders('R001',j)))
