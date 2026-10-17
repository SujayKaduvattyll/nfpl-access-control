CREATE TABLE IF NOT EXISTS branches (branch_id TEXT PRIMARY KEY, branch_name TEXT);
CREATE TABLE IF NOT EXISTS clients  (client_id TEXT PRIMARY KEY, client_name TEXT);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, user_name TEXT, user_type TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS roles (role_id TEXT PRIMARY KEY, role_name TEXT, description TEXT);
-- A role is just a named set of permissions. stages = '*' or 'A|B' (stages where the permission applies).
CREATE TABLE IF NOT EXISTS role_permissions (role_id TEXT, permission TEXT, stages TEXT DEFAULT '*',
  PRIMARY KEY (role_id, permission));
-- One row = "user U has role R for client C, branch B, direction D, mode M" (ALL = wildcard).
CREATE TABLE IF NOT EXISTS access_assignments (
  access_id TEXT PRIMARY KEY, user_id TEXT, role_id TEXT,
  client_id TEXT, branch_id TEXT, direction TEXT, mode TEXT,
  valid_from TEXT, valid_to TEXT, status TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS ix_assign_user ON access_assignments(user_id);
CREATE TABLE IF NOT EXISTS delegations (delegation_id TEXT PRIMARY KEY, from_user_id TEXT, to_user_id TEXT,
  valid_from TEXT, valid_to TEXT, created_by TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, client_id TEXT, branch_id TEXT, direction TEXT, mode TEXT, stage TEXT,
  responsible_user_id TEXT DEFAULT '', created_by TEXT, prepared_by TEXT DEFAULT '', checked_by TEXT DEFAULT '',
  filed_by TEXT DEFAULT '', created_at TEXT, updated_at TEXT, scenario TEXT DEFAULT '', notes TEXT DEFAULT '',
  attention TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_jobs_scope ON jobs(client_id, branch_id, direction, mode, stage);
CREATE INDEX IF NOT EXISTS ix_jobs_stage ON jobs(stage, responsible_user_id);
CREATE INDEX IF NOT EXISTS ix_jobs_resp  ON jobs(responsible_user_id);
CREATE INDEX IF NOT EXISTS ix_jobs_attn  ON jobs(attention);
-- Append-only history: allowed AND denied attempts, with the reason and the assignment that justified it.
CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts TEXT, job_id TEXT, actor_user_id TEXT, action TEXT,
  from_stage TEXT, to_stage TEXT, decision TEXT, reason TEXT, approver_user_id TEXT,
  reason_code TEXT DEFAULT '', assignment_id TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_audit_job ON audit_events(job_id, ts);
CREATE INDEX IF NOT EXISTS ix_audit_actor ON audit_events(actor_user_id, ts);
-- Two-person approvals (filed cancel, reopen, privileged access grants).
CREATE TABLE IF NOT EXISTS approval_requests (
  request_id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, job_id TEXT, stage_at_request TEXT,
  requester TEXT, reason TEXT, payload TEXT, status TEXT, created_ts TEXT, decided_by TEXT, decided_ts TEXT);
CREATE INDEX IF NOT EXISTS ix_appr_status ON approval_requests(status);
