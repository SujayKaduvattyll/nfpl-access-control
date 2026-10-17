"""Edge-case tests. Run:  python -m unittest discover -s tests -v
Every test loads the sample data fresh into an in-memory database with a fixed clock (2026-10-17)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nfpl.service import connect, load_data, Service
from nfpl.engine import decide, NEXT_ACTION, ACTIONS, STAGES

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
NOW = "2026-10-17T10:00:00"
# People (see data/ACCESS_SUMMARY.md): U001 Aarav broad Maker + Checker(Apex,Bharat); U002 Priya Checker(Coastal/Mumbai/Import/Air)
# U003 Rohan Checker(Delta/Delhi/Import); U007,U013 Managers; U014 Isha Maker; U017 Aditya (absent); U018 Kavya client user
# U019 Nikhil Admin; U020 overnight parser.


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        load_data(self.conn, DATA)
        self.s = Service(self.conn, clock=NOW)

    def ok(self, user, action, job, **kw):
        d = self.s.authorize(user, action, job)
        self.assertTrue(d.allowed, f"{user} {action} {job}: {d.code} {d.reason}")
        return d

    def no(self, user, action, job, code):
        d = self.s.authorize(user, action, job)
        self.assertFalse(d.allowed, f"{user} {action} {job} unexpectedly allowed")
        self.assertEqual(d.code, code, d.reason)
        return d

    def add_job(self, jid, client, branch, direction, mode, stage, resp="", prep="", chk="", created="2026-09-10T09:00"):
        self.conn.execute("INSERT INTO jobs (job_id,client_id,branch_id,direction,mode,stage,responsible_user_id,created_by,prepared_by,checked_by,filed_by,created_at,updated_at,scenario,notes,attention) VALUES (?,?,?,?,?,?,?,'U020',?,?,'',?,?,'','','')",
                          (jid, client, branch, direction, mode, stage, resp, prep, chk, created, created))
        self.s.refresh_attention([jid])


class TestScopeAndRoles(Base):
    def test_narrow_checker_allow_and_two_denies(self):          # Q2: Priya = Coastal + Mumbai + Import + Air
        self.ok("U002", "job.check", "J006")
        d = self.no("U002", "job.check", "J007", "DENY_SCOPE_MISMATCH")      # export
        self.assertIn("direction", d.reason)
        d = self.no("U002", "job.check", "J008", "DENY_SCOPE_MISMATCH")      # Delhi
        self.assertIn("branch", d.reason)

    def test_maker_everywhere_checker_for_few(self):             # Q2: Aarav
        self.ok("U001", "job.prepare", "J001")                    # Maker everywhere (job owned by him)
        self.no("U001", "job.check", "J006", "DENY_SCOPE_MISMATCH")  # Checker only for Apex/Bharat, not Coastal Foods

    def test_why_can_she_and_he_cant(self):                       # Q10
        r = self.s.compare("U002", "U001", "job.check", "J006")
        self.assertTrue(r["a"]["allowed"]); self.assertFalse(r["b"]["allowed"])
        self.assertIn("can because", r["summary"]); self.assertIn("cannot because", r["summary"])


class TestMakerChecker(Base):
    def test_preparer_cannot_check_own_work(self):                # Q3
        self.no("U001", "job.check", "J021", "DENY_SEPARATION_OF_DUTIES")

    def test_stuck_job_is_surfaced_not_bypassed(self):            # Q3 + Q14: nobody else is qualified
        self.assertEqual(self.s.job("J021")["attention"], "NO_ELIGIBLE_ACTOR")
        self.ok("U007", "job.view", "J021")                       # Manager sees it because it needs attention
        w = self.s.waiting_for("U007")
        self.assertIn("J021", [j["job_id"] for j in w["jobs"] if "job.reassign" in j["actions"]] or [j["job_id"] for j in w["jobs"]])

    def test_second_qualified_checker_can_act(self):
        self.add_job("JT01", "C009", "B005", "IMPORT", "AIR", "PREPARED", prep="U010")
        self.ok("U011", "job.check", "JT01")


class TestAbsentOwner(Base):
    def test_absent_user_cannot_act(self):                        # Q4
        self.no("U017", "job.prepare", "J016", "DENY_USER_NOT_ACTIVE")

    def test_absent_owner_does_not_block_qualified_colleague(self):
        self.assertEqual(self.s.job("J022")["attention"], "OWNER_ABSENT")
        self.ok("U003", "job.check", "J022")                      # Rohan: Delta/Delhi/Import checker

    def test_active_qualified_owner_blocks_others_until_manager_reassigns(self):
        self.add_job("JT03", "C001", "B002", "IMPORT", "FCL", "PREPARED", resp="U015", prep="U014")
        self.assertEqual(self.s.job("JT03")["attention"], "")
        self.ok("U015", "job.check", "JT03")                                    # the owner (Checker for Apex Delhi FCL imports)
        self.no("U001", "job.check", "JT03", "DENY_OWNED_BY_OTHER")             # qualified (Apex Checker) but not the owner
        self.no("U007", "job.reassign", "JT03", "DENY_NOT_VISIBLE")             # Managers don't see healthy in-progress jobs...
        self.assertTrue(self.s.set_user_status("U007", "U015", "absent")["ok"])  # ...they mark the owner absent, which flags the job
        self.assertEqual(self.s.job("JT03")["attention"], "OWNER_ABSENT")
        self.ok("U001", "job.check", "JT03")                                    # any qualified person may now act
        self.assertTrue(self.s.perform("U007", "job.reassign", "JT03", to_user="U001")["ok"])   # or the Manager reassigns it
        self.ok("U001", "job.check", "JT03")

    def test_returning_owner_who_is_not_qualified_does_not_block(self):
        self.s.set_user_status("U019", "U017", "active")                        # Aditya is back, but he is a Maker, not a Checker
        self.assertEqual(self.s.job("J022")["attention"], "OWNER_OUT_OF_SCOPE")
        self.ok("U003", "job.check", "J022")

    def test_delegation_does_not_grant_scope(self):               # Q4: delegation is not a back door
        self.no("U014", "job.check", "J016", "DENY_SCOPE_MISMATCH")  # Isha is the delegate (DG001) but has no Delta scope


class TestChangedResponsibility(Base):
    def test_revoke_takes_effect_immediately_and_flags_inflight_jobs(self):    # Q5
        self.add_job("JT10", "C009", "B005", "IMPORT", "AIR", "PREPARED", resp="U011", prep="U010")
        self.ok("U011", "job.check", "JT10")
        r = self.s.revoke_access("U019", "A013", "Rahul moved teams")
        self.assertTrue(r["ok"])
        self.no("U011", "job.check", "JT10", "DENY_SCOPE_MISMATCH")           # immediate (Rahul keeps only his Everest scope)
        self.assertEqual(self.s.job("JT10")["attention"], "NO_ELIGIBLE_ACTOR")  # in-flight job surfaces to a Manager
        self.ok("U007", "job.view", "JT10")

    def test_reassign_after_regrant(self):
        self.add_job("JT11", "C009", "B005", "IMPORT", "AIR", "PREPARED", resp="U011", prep="U010")
        self.s.revoke_access("U019", "A013")
        self.assertTrue(self.s.grant_access("U019", "U015", "R002", "C009", "B005", "IMPORT", "ALL")["ok"])
        r = self.s.perform("U007", "job.reassign", "JT11", to_user="U015")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.s.job("JT11")["attention"], "")
        self.ok("U015", "job.check", "JT11")

    def test_reassign_to_unqualified_person_is_refused(self):
        self.add_job("JT12", "C009", "B005", "IMPORT", "AIR", "PREPARED", resp="U011", prep="U010")
        self.s.revoke_access("U019", "A013")
        r = self.s.perform("U007", "job.reassign", "JT12", to_user="U016")
        self.assertFalse(r["ok"]); self.assertEqual(r["decision"]["code"], "DENY_TARGET_NOT_QUALIFIED")

    def test_expired_assignment_stops_working(self):
        self.s.expire_assignments()
        self.add_job("JT13", "C005", "B001", "IMPORT", "AIR", "PREPARED", prep="U004")
        self.no("U004", "job.check", "JT13", "DENY_NO_ROLE")                 # A026 (temporary Checker) ended 2026-09-30
        self.ok("U011", "job.check", "JT13")                                 # permanent Checker still works


class TestFiledJobs(Base):
    def test_filed_job_is_locked(self):                           # Q6
        self.no("U001", "job.edit", "J018", "DENY_STAGE")
        self.no("U001", "job.check", "J018", "DENY_STAGE")

    def test_only_managers_cancel_and_it_needs_two_people(self):
        self.no("U001", "job.cancel", "J018", "DENY_NO_ROLE")
        self.no("U018", "job.cancel", "J018", "DENY_NO_ROLE")
        r = self.s.perform("U007", "job.cancel", "J018", reason="")
        self.assertEqual(r["decision"]["code"], "DENY_REASON_REQUIRED")
        r = self.s.perform("U007", "job.cancel", "J018", reason="Client withdrew")
        self.assertEqual(r["status"], "PENDING_APPROVAL")
        self.assertEqual(self.s.job("J018")["stage"], "FILED")                # nothing happened yet
        self.assertEqual(self.s.approve("U007", r["request_id"])["decision"]["code"], "DENY_SELF_APPROVAL")
        self.assertEqual(self.s.approve("U019", r["request_id"])["ok"], False)  # an Admin is not a Manager
        done = self.s.approve("U013", r["request_id"])
        self.assertEqual(done["job"]["stage"], "CANCELLED")
        evs = [e["action"] for e in self.s.history("J018")]
        self.assertIn("job.cancel.request", evs); self.assertIn("job.cancel", evs)

    def test_reopen_needs_reason_and_second_manager_and_restarts_the_cycle(self):
        r = self.s.perform("U007", "job.reopen", "J018", reason="Client sent corrected invoice")
        self.assertEqual(r["status"], "PENDING_APPROVAL")
        self.assertEqual(self.s.approve("U013", r["request_id"])["job"]["stage"], "REOPENED")
        j = self.s.job("J018")
        self.assertEqual((j["prepared_by"], j["checked_by"], j["filed_by"]), ("", "", ""))   # must be re-prepared and re-checked
        self.ok("U001", "job.prepare", "J018")

    def test_filing_requires_someone_other_than_the_preparer(self):
        j = self.conn.execute("SELECT job_id FROM jobs WHERE stage='CHECKED' LIMIT 1").fetchone()[0]
        self.conn.execute("UPDATE jobs SET prepared_by='U007' WHERE job_id=?", (j,))
        self.no("U007", "job.file", j, "DENY_SEPARATION_OF_DUTIES")
        self.ok("U013", "job.file", j)

    def test_manager_sees_what_comes_after_maker_and_checker(self):   # D2
        self.ok("U007", "job.view", "J018")                                   # FILED
        self.add_job("JT30", "C009", "B005", "IMPORT", "AIR", "PREPARED", resp="U011", prep="U010")
        self.assertEqual(self.s.job("JT30")["attention"], "")
        self.no("U007", "job.view", "JT30", "DENY_NOT_VISIBLE")               # PREPARED with a healthy owner: not the Manager's yet
        self.ok("U007", "job.view", "J003")                                   # ...but J003's owner is a Maker, so it is flagged and shown


class TestAdmin(Base):
    def test_admin_cannot_grant_self_power(self):                 # Q8
        r = self.s.grant_access("U019", "U019", "R003")
        self.assertEqual(r["decision"]["code"], "DENY_SELF_GRANT")
        self.assertEqual(self.s.revoke_access("U019", "A021")["decision"]["code"], "DENY_SELF_GRANT")
        self.assertEqual(self.s.user_access("U019")["assignments"][0]["role_id"], "R005")

    def test_privileged_grant_to_someone_else_needs_a_manager(self):
        r = self.s.grant_access("U019", "U016", "R003", reason="cover")
        self.assertEqual(r["status"], "PENDING_APPROVAL")
        self.assertEqual(self.s.approve("U019", r["request_id"])["ok"], False)     # requester can't approve
        self.assertEqual(self.s.approve("U016", r["request_id"])["ok"], False)     # grantee can't approve
        self.assertTrue(self.s.approve("U007", r["request_id"])["ok"])
        self.assertTrue(any(a["role_id"] == "R003" for a in self.s.user_access("U016")["assignments"]))

    def test_ordinary_grant_is_immediate_and_audited(self):
        r = self.s.grant_access("U019", "U014", "R002", "C001", "B002", "EXPORT", "FCL", reason="extra cover")
        self.assertTrue(r["ok"]); self.ok("U014", "job.view", "J004")
        self.assertTrue(any(e["action"] == "access.grant" for e in self.conn.execute("SELECT * FROM audit_events WHERE actor_user_id='U019'").fetchall()))

    def test_admin_has_no_job_rights_and_non_admin_cannot_manage_access(self):
        self.no("U019", "job.check", "J006", "DENY_NO_ROLE")
        self.assertFalse(self.s.grant_access("U007", "U016", "R001")["ok"])
        self.assertFalse(self.s.grant_access("U001", "U001", "R003")["ok"])

    def test_new_role_is_data_not_code(self):                     # Q12
        self.assertTrue(self.s.create_role("U019", "R100", "Customs Reviewer", {"job.view": "*"})["ok"])
        self.assertFalse(self.s.create_role("U019", "R101", "Sneaky", {"access.manage": "*"})["ok"])     # no admin power via new roles
        self.assertFalse(self.s.create_role("U019", "R102", "Typo", {"job.delete": "*"})["ok"])
        self.s.grant_access("U019", "U016", "R100", "C003", "ALL", "ALL", "ALL")
        self.ok("U016", "job.view", "J006")
        self.no("U016", "job.check", "J006", "DENY_NO_ROLE")


class TestParser(Base):
    def test_parser_creates_and_updates_only_intake(self):        # Q9
        r = self.s.create_job("U020", "C003", "B001", "IMPORT", "AIR", "overnight batch")
        self.assertTrue(r["ok"]); jid = r["job"]["job_id"]
        self.assertEqual((r["job"]["stage"], r["job"]["created_by"]), ("CONFIRMED", "U020"))
        self.assertTrue(self.s.update_intake("U020", jid, "updated 02:10")["ok"])
        self.assertEqual(self.s.job(jid)["notes"], "updated 02:10")
        self.assertIn(jid, [j["job_id"] for j in self.s.waiting_for("U001")["jobs"]])    # appears for Makers

    def test_parser_cannot_do_human_work(self):
        for action in ("job.prepare", "job.check", "job.file", "job.cancel", "job.reopen", "job.reassign", "job.edit"):
            self.assertFalse(self.s.authorize("U020", action, "J001").allowed, action)
        self.no("U020", "job.update_intake", "J002", "DENY_NOT_VISIBLE")                  # PREPARED jobs are out of its reach
        self.assertFalse(self.s.update_intake("U020", "J002", "x")["ok"])
        self.assertFalse(self.s.grant_access("U020", "U020", "R003")["ok"])

    def test_parser_cannot_invent_a_job_outside_reality(self):
        self.assertFalse(self.s.create_job("U020", "C999", "B001", "IMPORT", "AIR")["ok"])
        self.assertFalse(self.s.create_job("U020", "ALL", "B001", "IMPORT", "AIR")["ok"])
        self.assertFalse(self.s.create_job("U001", "C001", "B001", "IMPORT", "AIR")["ok"])     # a Maker is not the parser


class TestClientUser(Base):
    def test_client_user_sees_only_own_client_and_cannot_act(self):   # Q7
        self.ok("U018", "job.view", "J001")
        self.no("U018", "job.view", "J006", "DENY_SCOPE_MISMATCH")
        for a in ("job.edit", "job.prepare", "job.check", "job.file", "job.cancel", "job.reopen"):
            self.assertFalse(self.s.authorize("U018", a, "J001").allowed, a)
        jobs = self.s.visible_jobs("U018", limit=500)["jobs"]
        self.assertTrue(jobs); self.assertTrue(all(j["client_id"] == "C001" for j in jobs))
        self.assertEqual(self.s.waiting_for("U018")["total"], 0)


class TestHistory(Base):
    def test_who_could_have_changed_on_the_3rd(self):             # Q11
        r = self.s.who_could_change("J021", "2026-10-03")
        names = {c["user_id"] for c in r["could_have_changed"]}
        self.assertIn("U001", names)
        self.assertNotIn("U007", names)      # Managers only see CHECKED onward (plus flagged jobs), so could not touch a fresh job
        self.assertNotIn("U018", names)       # client users can never change anything
        parser = [c for c in r["could_have_changed"] if c["user_id"] == "U020"][0]
        self.assertEqual(set(parser["can"]), {"job.update_intake"})      # the parser could only touch intake data
        self.assertTrue(any(e["actor_user_id"] == "U020" for e in r["actually_changed"]))     # parser created it that day
        self.assertTrue(any(e["decision"] == "DENY" for e in r["attempts_denied"]))            # Aarav's blocked self-check

    def test_could_have_changed_uses_the_assignments_of_that_day(self):
        self.add_job("JT20", "C005", "B001", "IMPORT", "AIR", "PREPARED", prep="U001")
        self.conn.execute("INSERT INTO audit_events (event_id,ts,job_id,actor_user_id,action,from_stage,to_stage,decision,reason,approver_user_id) VALUES ('X','2026-09-10T09:00','JT20','U020','job.create','','CONFIRMED','ALLOW','','')")
        self.conn.execute("INSERT INTO audit_events (event_id,ts,job_id,actor_user_id,action,from_stage,to_stage,decision,reason,approver_user_id) VALUES ('X','2026-09-10T11:00','JT20','U001','job.prepare','CONFIRMED','PREPARED','ALLOW','','')")
        sep = {c["user_id"] for c in self.s.who_could_change("JT20", "2026-09-15")["could_have_changed"]}
        octo = {c["user_id"] for c in self.s.who_could_change("JT20", "2026-10-03")["could_have_changed"]}
        self.assertIn("U004", sep)          # Neha was a temporary Checker until 2026-09-30
        self.assertNotIn("U004", octo)

    def test_every_decision_attempt_is_in_the_job_history(self):
        self.s.perform("U001", "job.check", "J021")
        h = self.s.history("J021")
        last = h[-1]
        self.assertEqual((last["decision"], last["reason_code"], last["actor_user_id"]), ("DENY", "DENY_SEPARATION_OF_DUTIES", "U001"))


class TestQueueMatchesAuthorization(Base):
    """Q13: the 'waiting for me' list must be exactly what the user can actually do."""
    def brute(self, uid):
        c, out = self.s.ctx(uid), set()
        for r in self.conn.execute("SELECT * FROM jobs"):
            j = dict(r)
            nxt = NEXT_ACTION.get(j["stage"])
            if nxt and decide(c, nxt, j).allowed:
                out.add(j["job_id"])
            elif j["attention"] and j["stage"] in ("CONFIRMED", "PREPARED", "CHECKED", "REOPENED") and decide(c, "job.reassign", j).allowed:
                out.add(j["job_id"])
        return out

    def test_queue_equals_brute_force_for_every_user(self):
        for (u,) in self.conn.execute("SELECT user_id FROM users").fetchall():
            q = self.s.waiting_for(u, limit=1000)
            self.assertEqual({j["job_id"] for j in q["jobs"]}, self.brute(u), u)
            for j in q["jobs"]:
                self.assertTrue(any(self.s.authorize(u, a, j["job_id"]).allowed for a in j["actions"]), (u, j))

    def test_visible_list_equals_brute_force_visibility(self):
        for (u,) in self.conn.execute("SELECT user_id FROM users").fetchall():
            c = self.s.ctx(u)
            expect = {r["job_id"] for r in self.conn.execute("SELECT * FROM jobs") if decide(c, "job.view", dict(r)).allowed}
            got = {j["job_id"] for j in self.s.visible_jobs(u, limit=1000)["jobs"]}
            self.assertEqual(got, expect, u)

    def test_queue_changes_the_moment_access_changes(self):
        before = self.s.waiting_for("U011")["count"]
        self.s.revoke_access("U019", "A013")
        self.assertLessEqual(self.s.waiting_for("U011")["count"], before)
        self.assertEqual(self.s.waiting_for("U011")["count"], len(self.brute("U011")))


class TestFailClosed(Base):
    def test_unknowns_are_denied(self):                           # Q14
        self.no("U999", "job.view", "J001", "DENY_UNKNOWN_USER")
        self.no("U001", "job.delete", "J001", "DENY_UNKNOWN_ACTION")
        self.no("U001", "job.view", "J99999", "DENY_UNKNOWN_JOB")

    def test_malformed_assignment_is_ignored_and_denies_with_reason(self):
        self.conn.execute("INSERT INTO access_assignments VALUES ('AX1','U016','R002','C002','B001','SIDEWAYS','ALL','2026-01-01','','ACTIVE','typo')")
        d = self.s.authorize("U016", "job.check", "J005")
        self.assertFalse(d.allowed)
        self.assertIn("AX1", self.s.user_access("U016")["malformed_ignored"])

    def test_unknown_stage_is_denied(self):
        self.conn.execute("UPDATE jobs SET stage='ARCHIVED' WHERE job_id='J001'")
        self.no("U001", "job.view", "J001", "DENY_UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
