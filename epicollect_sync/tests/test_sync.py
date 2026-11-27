"""Integration tests on a real site, with a fake Epicollect API. Run with:
    bench --site <test site> run-tests --app epicollect_sync
The sync commits and creates DocTypes (DDL), so each test uses its own project and DocType names.
"""

import json
from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from epicollect_sync import api as A
from epicollect_sync import fields as F
from epicollect_sync import notify, sync as S, targets as T
from epicollect_sync.tests.fakeapi import FakeAPI, ts

ANSWERS = {
    "1_age": "34",
    "2_dob": "1990-03-05T00:00:00.000",
    "3_loc": {"latitude": 16.5062, "longitude": 80.648, "accuracy": 12},
    "7_name": "Ravi",
}


class SyncTestCase(FrappeTestCase):
    def setUp(self):
        self.slug = "t-" + frappe.generate_hash(length=8).lower()
        self.prefix = "T " + self.slug[2:].upper()
        self.project = frappe.get_doc(
            {
                "doctype": "Epicollect Project",
                "project_slug": self.slug,
                "client_id": "cid",
                "client_secret": "secret",
                "per_page": 3,
                "doctype_prefix": self.prefix,
                "check_deletions": "Never",
                "notify_emails": "alerts@example.com",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.api = FakeAPI()
        for p in (patch.object(A, "_session", self.api), patch.object(A.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    # helpers
    def run_sync(self, **kw):
        S.run_sync(self.slug, **kw)
        return frappe.get_last_doc("Epicollect Sync Log", filters={"project": self.slug})

    def targets(self):
        return {(t.form_ref, t.branch_ref or ""): t for t in T.project_targets(self.slug)}

    def dt(self, form="F1", branch=""):
        return self.targets()[(form, branch)].target_doctype

    def row(self, uuid, form="F1", branch=""):
        return frappe.get_doc(self.dt(form, branch), uuid)

    def tfield(self, key, part="value", form="F1", branch=""):
        return json.loads(self.targets()[(form, branch)].typed_map)[key]["fields"][part]

    def seed(self, n=5):
        for i in range(n):
            self.api.add("F1", f"{self.slug}-h{i}", ts(i * 2), **{**ANSWERS, "1_age": str(30 + i)})


class TestSetUp(SyncTestCase):
    def test_creates_targets_doctypes_fields_and_dashboard(self):
        S.run_setup(self.slug)
        t = self.targets()
        self.assertEqual(set(t), {("F1", ""), ("F1", "F1_br"), ("F2", "")})
        self.assertEqual(t[("F1", "")].target_doctype, f"{self.prefix} Household")
        self.assertEqual(t[("F2", "")].target_doctype, f"{self.prefix} Member")
        self.assertEqual(t[("F1", "F1_br")].target_doctype, f"{self.prefix} Household Livestock")
        meta = frappe.get_meta(f"{self.prefix} Household")
        self.assertEqual(meta.get_field(self.tfield("1_age")).fieldtype, "Int")
        self.assertEqual(meta.get_field(self.tfield("2_dob")).fieldtype, "Date")
        self.assertEqual(meta.get_field(self.tfield("3_loc", "lat")).precision, "7")
        self.assertEqual(meta.get_field("q_7_name").label, "Head name")
        self.assertIsNone(meta.get_field("q_4_br"))  # branch container has no answer
        self.assertIsNone(meta.get_field("q_5_kind"))  # branch questions live on the branch DocType
        self.assertEqual(
            frappe.get_meta(f"{self.prefix} Member").get_field("parent_entry").options, f"{self.prefix} Household"
        )
        self.assertEqual(t[("F1", "")].location_key, "3_loc")
        dash = frappe.get_doc("Dashboard", f"Epicollect {self.slug}")
        self.assertEqual(len(dash.cards), 6)
        self.assertEqual(len(dash.charts), 4)
        # set-up again creates nothing new
        S.run_setup(self.slug)
        self.assertEqual(len(T.project_targets(self.slug)), 3)
        self.assertEqual(len(frappe.get_doc("Dashboard", f"Epicollect {self.slug}").charts), 4)

    def test_viewer_role_can_read(self):
        S.run_setup(self.slug)
        perms = {p.role for p in frappe.get_meta(self.dt()).permissions}
        self.assertIn(T.VIEWER, perms)

    def test_ascii_doctype_names_for_non_latin_form_names(self):
        self.api.project["data"]["project"]["forms"][1]["name"] = "సభ్యుడు"
        S.run_setup(self.slug)
        self.assertEqual(self.dt("F2"), f"{self.prefix} Form 2")


class TestSync(SyncTestCase):
    def test_first_sync_sets_up_and_fills_everything(self):
        self.seed()
        self.api.add(
            "F2", f"{self.slug}-m0", ts(20), ec5_parent_uuid=f"{self.slug}-h1", **{"1_mname": "Asha", "2_mage": "9"}
        )
        self.api.add(
            "F1",
            f"{self.slug}-b0",
            ts(21),
            branch="F1_br",
            ec5_branch_owner_uuid=f"{self.slug}-h0",
            **{"5_kind": "Cow", "6_n": "3"},
        )
        log = self.run_sync()
        self.assertEqual(log.status, "Success", log.error)
        self.assertEqual(log.inserted, 7)
        self.assertEqual(len(log.details), 3)
        h = self.row(f"{self.slug}-h2")
        self.assertEqual(h.get(self.tfield("1_age")), 32)
        self.assertEqual(h.get(self.tfield("2_dob")), date(1990, 3, 5))
        self.assertAlmostEqual(h.latitude, 16.5062)
        self.assertEqual(h.q_1_age, "32")
        self.assertEqual(h.project, self.slug)
        self.assertEqual(self.row(f"{self.slug}-m0", "F2").parent_entry, f"{self.slug}-h1")
        b = self.row(f"{self.slug}-b0", "F1", "F1_br")
        self.assertEqual(
            (b.parent_entry, b.q_5_kind, b.get(self.tfield("6_n", form="F1", branch="F1_br"))),
            (f"{self.slug}-h0", "Cow", 3),
        )
        self.assertEqual(self.targets()[("F1", "")].entry_count, 5)

    def test_incremental_edit_and_cleared_answer(self):
        self.seed()
        self.run_sync()
        log = self.run_sync()
        self.assertEqual((log.inserted, log.updated), (0, 0))
        e = self.api.data[("F1", None)][f"{self.slug}-h4"]
        e.pop("7_name")
        e["uploaded_at"] = ts(30)
        log = self.run_sync()
        self.assertEqual(log.updated, 1)
        self.assertIsNone(self.row(f"{self.slug}-h4").q_7_name)

    def test_edit_during_run_does_not_skip_entries(self):
        for i in range(9):
            self.api.add("F1", f"{self.slug}-e{i}", ts(i * 5))

        def edit(call_no):
            if call_no == 3:  # definition, then page 1 of F1 read; an entry on page 1 moves to the end
                self.api.data[("F1", None)][f"{self.slug}-e0"]["uploaded_at"] = ts(55)

        self.api.on_request = edit
        log = self.run_sync()
        self.assertEqual(log.status, "Success", log.error)
        self.assertEqual(frappe.db.count(self.dt()), 9)

    def test_many_entries_with_one_timestamp(self):
        for i in range(8):
            self.api.add("F1", f"{self.slug}-s{i}", ts(1))
        self.assertEqual(self.run_sync().status, "Success")
        self.assertEqual(frappe.db.count(self.dt()), 8)

    def test_unsorted_response_stops_without_moving_checkpoint(self):
        self.seed(3)
        real = self.api.get

        def reversed_get(*a, **k):
            r = real(*a, **k)
            if "entries" in (r._b.get("data") or {}):
                r._b["data"]["entries"].reverse()
            return r

        with patch.object(self.api, "get", reversed_get):
            log = self.run_sync()
        self.assertEqual(log.status, "Failed")
        self.assertIn("sorted by uploaded_at", log.error)
        self.assertFalse(self.targets()[("F1", "")].last_uploaded_at)

    def test_long_title_and_bad_date(self):
        self.api.add("F1", f"{self.slug}-x", ts(1), **{**ANSWERS, "2_dob": "rubbish"})
        self.api.data[("F1", None)][f"{self.slug}-x"].update(title="x" * 600, created_at="not a date")
        self.assertEqual(self.run_sync().status, "Success")
        r = self.row(f"{self.slug}-x")
        self.assertEqual(len(r.entry_title), 600)
        self.assertIsNone(r.created_at)
        self.assertIsNone(r.get(self.tfield("2_dob")))
        self.assertEqual(r.q_2_dob, "rubbish")

    def test_new_question_and_new_form_added_later(self):
        self.seed(2)
        self.run_sync()
        proj = self.api.project
        proj["data"]["project"]["forms"][0]["inputs"].append({"ref": "F1_inc", "type": "decimal", "question": "Income"})
        proj["meta"]["project_mapping"][0]["forms"]["F1"]["F1_inc"] = {"map_to": "8_inc"}
        proj["data"]["project"]["forms"].append(
            {"ref": "F3", "name": "Visit", "inputs": [{"ref": "F3_d", "type": "date", "question": "Visit date"}]}
        )
        proj["meta"]["project_mapping"][0]["forms"]["F3"] = {"F3_d": {"map_to": "1_vd"}}
        self.api.add("F1", f"{self.slug}-new", ts(40), **{**ANSWERS, "8_inc": "1200.50"})
        self.api.add(
            "F3", f"{self.slug}-v0", ts(41), ec5_parent_uuid=f"{self.slug}-m0", **{"1_vd": "2026-02-01T00:00:00.000"}
        )
        log = self.run_sync()
        self.assertEqual(log.status, "Success", log.error)
        self.assertEqual(self.row(f"{self.slug}-new").get(self.tfield("8_inc")), 1200.5)
        self.assertEqual(frappe.get_meta(self.dt()).get_field(self.tfield("8_inc")).label, "Income (number)")
        self.assertEqual(self.row(f"{self.slug}-v0", "F3").get(self.tfield("1_vd", form="F3")), date(2026, 2, 1))
        self.assertEqual(frappe.get_meta(self.dt("F3")).get_field("parent_entry").options, self.dt("F2"))

    def test_failure_in_one_target_does_not_stop_others(self):
        self.seed(2)
        self.api.add("F2", f"{self.slug}-m0", ts(20), **{"1_mname": "A"})
        self.api.fail_when = lambda params: params.get("branch_ref") == "F1_br"
        log = self.run_sync()
        self.assertEqual(log.status, "Failed")
        notes = {d.target_doctype: d.note for d in log.details}
        self.assertIn("Failed", notes[self.dt("F1", "F1_br")])
        self.assertEqual(frappe.db.count(self.dt("F2")), 1)
        self.assertEqual(frappe.db.count(self.dt()), 2)

    def test_retries_bounded(self):
        self.seed(1)
        self.api.fail = [503, 429]
        self.assertEqual(self.run_sync().status, "Success")
        self.api.fail = [503] * 200
        S.reset_checkpoints(self.slug)
        self.assertEqual(self.run_sync().status, "Failed")

    def test_stale_running_logs_closed(self):
        stale = frappe.get_doc(
            {
                "doctype": "Epicollect Sync Log",
                "project": self.slug,
                "status": "Running",
                "started_at": add_to_date(now_datetime(), hours=-5),
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
        self.assertEqual(self.run_sync().status, "Success")
        self.assertEqual(frappe.db.get_value("Epicollect Sync Log", stale.name, "status"), "Failed")

    def test_missing_definition_on_first_run_fails_clearly(self):
        self.api.project = None
        log = self.run_sync()
        self.assertEqual(log.status, "Failed")
        self.assertIn("project definition", log.error)


class TestDeletions(SyncTestCase):
    def test_full_check_flags_and_restores(self):
        self.seed(6)
        self.run_sync()
        gone = self.api.data[("F1", None)].pop(f"{self.slug}-h3")
        log = self.run_sync(full_check=True)
        self.assertEqual((log.status, log.deleted, log.full_check), ("Success", 1, 1), log.error)
        self.assertEqual(self.row(f"{self.slug}-h3").is_deleted, 1)
        self.assertEqual(self.targets()[("F1", "")].entry_count, 5)
        self.api.data[("F1", None)][f"{self.slug}-h3"] = gone
        log = self.run_sync(full_check=True)
        self.assertEqual(self.row(f"{self.slug}-h3").is_deleted, 0)
        self.assertEqual(log.updated, 1)

    def test_full_check_guard_when_api_returns_too_few(self):
        self.seed(6)
        self.run_sync()
        for i in range(5):
            self.api.data[("F1", None)].pop(f"{self.slug}-h{i}")
        log = self.run_sync(full_check=True)
        self.assertEqual(log.deleted, 0)
        self.assertIn("skipped", next(d.note for d in log.details if d.target_doctype == self.dt()))

    def test_full_check_due_by_schedule(self):
        self.project.db_set("check_deletions", "Daily")
        self.seed(2)
        self.assertEqual(self.run_sync().full_check, 1)
        self.assertEqual(self.run_sync().full_check, 0)


class TestScheduleAndNotify(SyncTestCase):
    def test_scheduler_respects_interval(self):
        self.project.db_set({"enabled": 1, "sync_every": "60"})
        frappe.db.commit()
        queued = []
        with (
            patch.object(S.frappe, "enqueue", lambda *a, **k: queued.append(k.get("project"))),
            patch.object(S, "is_job_enqueued", lambda j: False),
        ):
            S.scheduled()
            self.assertIn(self.slug, queued)
            frappe.get_doc(
                {
                    "doctype": "Epicollect Sync Log",
                    "project": self.slug,
                    "status": "Success",
                    "started_at": add_to_date(now_datetime(), minutes=-30),
                }
            ).insert(ignore_permissions=True)
            queued.clear()
            S.scheduled()
            self.assertNotIn(self.slug, queued)

    def test_enqueue_reports_busy(self):
        with patch.object(S, "is_job_enqueued", lambda j: True):
            res = S.sync_now(self.slug)
        self.assertFalse(res["queued"])

    def setUp(self):
        super().setUp()
        self.sent = []
        p = patch.object(notify.frappe, "sendmail", lambda **k: self.sent.append(k))
        p.start()
        self.addCleanup(p.stop)

    def _emails(self):
        return [m for m in self.sent if m["reference_name"] == self.slug]

    def test_missing_email_account_is_logged_not_raised(self):
        def boom(**k):
            raise frappe.OutgoingEmailError("no account")

        with patch.object(notify.frappe, "sendmail", boom):
            notify._send(frappe.get_doc("Epicollect Project", self.slug), "s", "m")

    def test_failure_and_recovery_emails_once_each(self):
        self.seed(1)
        self.api.fail = [503] * 200
        self.run_sync()
        self.run_sync()
        self.assertEqual(len(self._emails()), 1)
        self.api.fail = []
        self.assertEqual(self.run_sync().status, "Success")
        self.assertEqual(len(self._emails()), 2)

    def test_inactivity_alert(self):
        self.project.db_set({"enabled": 1, "inactivity_days": 3})
        frappe.get_doc(
            {
                "doctype": "Epicollect Sync Log",
                "project": self.slug,
                "status": "Success",
                "inserted": 0,
                "started_at": now_datetime(),
            }
        ).insert(ignore_permissions=True)
        notify.check_inactivity()
        self.assertEqual(len(self._emails()), 1)


class TestDashboards(SyncTestCase):
    def test_charts_and_cards_compute(self):
        from frappe.desk.doctype.dashboard_chart.dashboard_chart import get as get_chart
        from frappe.desk.doctype.number_card.number_card import get_result

        self.seed(4)
        self.run_sync()
        dt = self.dt()
        card = frappe.get_doc("Number Card", f"{dt}: Entries")
        self.assertEqual(get_result(card, card.filters_json), 4)
        data = get_chart(chart_name=f"{dt}: Entries by collector", refresh=1)
        self.assertEqual(data["datasets"][0]["values"], [4])


class TestValidation(SyncTestCase):
    def test_project_validation(self):
        p = frappe.get_doc("Epicollect Project", self.slug)
        p.project_slug = "https://five.epicollect.net/project/" + self.slug
        p.save()
        self.assertEqual(p.project_slug, self.slug)
        p.notify_emails = "ok@example.com, not-an-email"
        self.assertRaises(frappe.ValidationError, p.save)


class TestMigration(FrappeTestCase):
    def test_settings_to_project(self):
        from frappe.utils.password import set_encrypted_password

        from epicollect_sync.patches.v1_0 import settings_to_project

        slug = "legacy-" + frappe.generate_hash(length=6).lower()
        vals = {
            "project_slug": slug,
            "client_id": "old",
            "enabled": "1",
            "per_page": "200",
            "last_uploaded_at": "2026-01-01T00:00:00.000Z",
            "field_map": '{"1_age": "q_1_age"}',
            "typed_map": '{"3_loc": {"kind": "location", "fields": {}}}',
        }
        for k, v in vals.items():
            frappe.db.sql(
                "insert into tabSingles (doctype, field, value) values ('Epicollect Settings', %s, %s)", (k, v)
            )
        set_encrypted_password("Epicollect Settings", "Epicollect Settings", "s3cret", "client_secret")
        frappe.get_doc({"doctype": "Epicollect Entry", "ec5_uuid": slug + "-1"}).insert(ignore_permissions=True)
        settings_to_project.execute()
        p = frappe.get_doc("Epicollect Project", slug)
        self.assertEqual(
            (p.client_id, p.get_password("client_secret"), p.per_page, p.enabled), ("old", "s3cret", 200, 1)
        )
        t = T.project_targets(slug)[0]
        self.assertEqual(
            (t.target_doctype, t.last_uploaded_at, t.location_key),
            ("Epicollect Entry", "2026-01-01T00:00:00.000Z", "3_loc"),
        )
        self.assertEqual(frappe.db.get_value("Epicollect Entry", slug + "-1", "project"), slug)
        self.assertFalse(frappe.db.sql("select 1 from tabSingles where doctype='Epicollect Settings'"))
        frappe.db.rollback()


class TestParsing(FrappeTestCase):
    def test_questions_for_form_and_branch(self):
        d = FakeAPI().project
        q = A.questions(d, "F1")
        self.assertEqual(list(q), ["1_age", "2_dob", "3_loc", "7_name"])
        self.assertEqual(q["2_dob"]["datetime_format"], "dd/MM/YYYY")
        b = A.questions(d, "F1", "F1_br")
        self.assertEqual(list(b), ["5_kind", "6_n"])
        self.assertEqual([x["ref"] for x in A.branches(A.forms(d)[0])], ["F1_br"])

    def test_dates(self):
        cases = [
            ("2024-01-31T00:00:00.000", False, date(2024, 1, 31)),
            ("05/03/2024", True, date(2024, 3, 5)),
            ("05/03/2024", False, date(2024, 5, 3)),
            ("2024-02-30", False, None),
            ("x", False, None),
        ]
        for v, dayfirst, expect in cases:
            self.assertEqual(F.to_date(v, dayfirst), expect, v)

    def test_numbers_and_locations(self):
        self.assertEqual(F.num("12", "int"), 12)
        self.assertIsNone(F.num("99999999999", "int"))
        self.assertIsNone(F.num("nan", "number"))
        self.assertEqual(F.location('{"latitude": 1, "longitude": 2}'), {"lat": 1.0, "lng": 2.0, "acc": None})
        self.assertEqual(F.location({"latitude": 95, "longitude": 2}), {})

    def test_fieldnames(self):
        self.assertEqual(F.fieldname("AGE", {"q_age"}), "q_age_2")
        self.assertLessEqual(len(F.fieldname("x" * 100, set())), 55)
        self.assertTrue(F.is_core("ec5_branch_owner_uuid"))
