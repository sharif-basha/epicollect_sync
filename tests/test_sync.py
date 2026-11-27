import pytest
import requests


def ts(i):
    return f"2026-01-01T10:00:{i:02d}.000Z"


def rows(env):
    return env.db.rows["Epicollect Entry"]


def last_log(env):
    return list(env.db.rows["Epicollect Sync Log"].values())[-1]


def test_full_then_incremental(env):
    for i in range(7):
        env.api.add(f"u{i}", ts(i * 5), **{"1_age": str(i)})
    env.sync.run_sync()
    log = last_log(env)
    assert log["status"] == "Success", log["error"]
    assert len(rows(env)) == 7 and log["inserted"] == 7
    assert env.db.single["last_uploaded_at"] == ts(30)
    fn = env.sync._map()["1_age"]
    assert rows(env)["u3"][fn] == "3"

    # second run: only the overlap is re-fetched, nothing changes
    env.sync.run_sync()
    log = last_log(env)
    assert log["status"] == "Success" and log["inserted"] == 0 and log["updated"] == 0

    # an edit is picked up and a removed answer is cleared
    env.api.entries["u6"].pop("1_age")
    env.api.entries["u6"]["uploaded_at"] = ts(40)
    env.sync.run_sync()
    assert last_log(env)["updated"] == 1
    assert rows(env)["u6"][fn] is None


def test_edit_during_run_does_not_skip_entries(env):
    """B1: with page numbers, an entry edited mid-run shifts the pages and another entry is never fetched."""
    for i in range(9):
        env.api.add(f"u{i}", ts(i * 5))

    def edit(call_no):
        if call_no == 2:  # after page 1 was read, an entry on it is edited and moves to the end
            env.api.entries["u0"]["uploaded_at"] = ts(55)
    env.api.on_request = edit
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"
    assert set(rows(env)) == {f"u{i}" for i in range(9)}
    assert rows(env)["u0"]["uploaded_at"].second == 55


def test_many_entries_with_same_timestamp(env):
    for i in range(8):
        env.api.add(f"u{i}", ts(1))
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"
    assert len(rows(env)) == 8


def test_unsorted_response_stops_without_moving_checkpoint(env, monkeypatch):
    """B2: if the API ignores sort_by=uploaded_at the checkpoint would jump past unfetched entries."""
    for i in range(3):
        env.api.add(f"u{i}", ts(i))
    real = env.api.get

    def reversed_get(*a, **k):
        r = real(*a, **k)
        r._body["data"]["entries"].reverse()
        return r
    monkeypatch.setattr(env.api, "get", reversed_get)
    env.sync.run_sync()
    log = last_log(env)
    assert log["status"] == "Failed" and "sorted by uploaded_at" in log["error"]
    assert env.db.single["last_uploaded_at"] == ""


def test_long_title_and_bad_date_do_not_crash(env):
    """B3 / B13."""
    env.api.add("u1", ts(1))
    env.api.entries["u1"]["title"] = "x" * 500
    env.api.entries["u1"]["created_at"] = "not a date"
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"
    assert len(rows(env)["u1"]["entry_title"]) == 500
    assert rows(env)["u1"]["created_at"] is None


def test_retries_are_bounded_and_honour_server_errors(env):
    """B4: 5xx and 429 are retried; a persistent failure ends the run instead of hanging."""
    env.api.add("u1", ts(1))
    env.api.fail = [503, 429]
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"

    env.api.fail = [503] * 50
    env.db.single["last_uploaded_at"] = ""
    env.sync.run_sync()
    assert last_log(env)["status"] == "Failed"


def test_connection_errors_retried(env, monkeypatch):
    env.api.add("u1", ts(1))
    real, state = env.api.get, {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.ConnectionError("reset")
        return real(*a, **k)
    monkeypatch.setattr(env.api, "get", flaky)
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"


def test_stale_running_logs_are_closed(env):
    """B5."""
    env.db.rows["Epicollect Sync Log"]["SYNC-00000"] = {"name": "SYNC-00000", "status": "Running"}
    env.sync.run_sync()
    assert env.db.rows["Epicollect Sync Log"]["SYNC-00000"]["status"] == "Failed"
    assert last_log(env)["status"] == "Success"


def test_enqueue_reports_busy(env):
    """B6: the user is told when a job is already running instead of a false 'queued'."""
    assert env.sync.sync_now()["queued"] is True
    env.frappe.job_running = True
    res = env.sync.create_fields()
    assert res["queued"] is False and "already" in res["message"]
    assert len(env.frappe.enqueued) == 1


def test_missing_settings_fail_clearly(env):
    env.frappe.settings.vals["project_slug"] = ""
    env.sync.run_sync()
    log = last_log(env)
    assert log["status"] == "Failed" and "Project Slug" in log["error"]
    assert env.api.calls == []


def test_token_cache_keyed_by_credentials(env):
    s = env.frappe.settings
    k1 = env.sync._token_key(s)
    s.vals["client_id"] = "other"
    assert env.sync._token_key(s) != k1


def test_labels_from_definition_strip_html(env):
    env.api.add("u1", ts(1), **{"1_age": "4"})
    env.sync.run_sync()
    assert env.frappe.custom_fields[0]["label"] == "Age?"


def test_unchanged_pages_cost_one_query(env):
    for i in range(3):
        env.api.add(f"u{i}", ts(i))
    env.sync.run_sync()
    before = env.db.queries
    n = env.sync._save_page([dict(e) for e in env.api.entries.values()], env.sync._map())
    assert n == {"inserted": 0, "updated": 0, "unchanged": 3}
    assert env.db.queries - before == 1


@pytest.mark.parametrize("key,expect", [("1_Name", "q_1_name"), ("Age", "q_age")])
def test_fieldname(env, key, expect):
    assert env.sync._fieldname(key, set()) == expect


def test_fieldname_collisions_and_length(env):
    used = {"q_age"}
    assert env.sync._fieldname("AGE", used) == "q_age_2"
    long = env.sync._fieldname("x" * 100, set())
    assert len(long) <= 55


# ---------------------------------------------------------------- typed fields
from datetime import date  # noqa: E402

ANSWERS = {"1_age": "34", "2_inc": "1250.5", "3_dob": "1990-03-05T00:00:00.000",
           "4_loc": {"latitude": 16.5062, "longitude": 80.648, "accuracy": 12}, "6_n": "5", "9_name": "Ravi"}


def by_label(env):
    return {f["label"]: f for f in env.frappe.custom_fields}


def test_definition_parsing(env):
    d = env.sync._parse_definition(env.api.project)
    assert d["1_age"] == {"label": "Age?", "type": "integer", "datetime_format": None}
    assert d["6_n"]["type"] == "integer"  # inside a group
    assert "8_vd" not in d                 # inside a branch: not part of the form's entries
    assert env.sync._parse_definition(env.api.project, map_index="1") == {
        "age": {"label": "Age?", "type": "integer", "datetime_format": None}}


def test_definition_fallback_when_shape_unknown(env):
    env.api.project = {"something": [{"ref": "r", "question": "Q"}, {"r": {"map_to": "k"}}]}
    assert env.sync._definition(env.frappe.settings) == {"k": {"label": "Q"}}


def test_sync_creates_and_fills_typed_columns(env):
    env.api.add("u1", ts(1), **ANSWERS)
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success", last_log(env)["error"]
    f = by_label(env)
    assert f["Age? (number)"]["fieldtype"] == "Int"
    assert f["Income (number)"]["fieldtype"] == "Float"
    assert f["Date of birth (date)"]["fieldtype"] == "Date"
    assert f["Location (latitude)"]["precision"] == "7"
    assert "Name (number)" not in f and "Name" in f
    # typed columns sit right after their text field
    names = [x["fieldname"] for x in env.frappe.custom_fields]
    assert names.index(f["Age? (number)"]["fieldname"]) == names.index(f["Age?"]["fieldname"]) + 1
    r = rows(env)["u1"]
    assert r[f["Age? (number)"]["fieldname"]] == 34
    assert r[f["Income (number)"]["fieldname"]] == 1250.5
    assert r[f["Date of birth (date)"]["fieldname"]] == date(1990, 3, 5)
    assert r[f["Location (latitude)"]["fieldname"]] == 16.5062
    assert r[f["Location (accuracy m)"]["fieldname"]] == 12
    assert r[f["Members (number)"]["fieldname"]] == 5
    assert r[f["Age?"]["fieldname"]] == "34"  # text field keeps the answer as received


def test_bad_values_leave_typed_columns_empty(env):
    env.api.add("u1", ts(1), **{**ANSWERS, "1_age": "abc", "2_inc": "", "3_dob": "",
                                  "4_loc": {"latitude": "", "longitude": "", "accuracy": ""}, "6_n": "99999999999"})
    env.sync.run_sync()
    assert last_log(env)["status"] == "Success"
    r, f = rows(env)["u1"], by_label(env)
    for label in ("Age? (number)", "Income (number)", "Date of birth (date)", "Location (latitude)",
                  "Members (number)"):
        assert r[f[label]["fieldname"]] is None, label
    assert r[f["Age?"]["fieldname"]] == "abc"


@pytest.mark.parametrize("v,dayfirst,expect", [
    ("2024-01-31T00:00:00.000", False, date(2024, 1, 31)),
    ("2024-01-31", True, date(2024, 1, 31)),
    ("05/03/2024", True, date(2024, 3, 5)),
    ("05/03/2024", False, date(2024, 5, 3)),
    ("2024-02-30T00:00:00", False, None),
    ("rubbish", False, None),
    (None, False, None),
])
def test_date_parsing(env, v, dayfirst, expect):
    assert env.sync._date(v, dayfirst) == expect


@pytest.mark.parametrize("v,expect", [
    ({"latitude": "16.5", "longitude": "80.6", "accuracy": "5"}, {"lat": 16.5, "lng": 80.6, "acc": 5.0}),
    ('{"latitude": 16.5, "longitude": 80.6}', {"lat": 16.5, "lng": 80.6, "acc": None}),
    ({"latitude": 95, "longitude": 80}, {}),
    ({"latitude": "", "longitude": ""}, {}),
    ("not json", {}),
])
def test_location_parsing(env, v, expect):
    assert env.sync._location(v) == expect


def test_create_fields_backfills_existing_entries(env):
    """An install synced before typed columns existed: Create Fields adds them and fills old rows."""
    env.api.project = None  # definition unavailable: only text fields are created
    for i in range(3):
        env.api.add(f"u{i}", ts(i), **{**ANSWERS, "1_age": str(30 + i)})
    env.sync.run_sync()
    assert not any(x["fieldtype"] == "Int" for x in env.frappe.custom_fields)
    n_text = len(env.frappe.custom_fields)

    from tests.conftest import DEFINITION
    env.api.project = DEFINITION
    env.sync.build_fields()
    assert len([x for x in env.frappe.custom_fields if x["fieldtype"] == "Small Text"]) == n_text  # no duplicates
    age = env.sync._tmap()["1_age"]["fields"]["value"]
    assert [rows(env)[f"u{i}"][age] for i in range(3)] == [30, 31, 32]
    lat = env.sync._tmap()["4_loc"]["fields"]["lat"]
    assert rows(env)["u0"][lat] == 16.5062  # location parsed back from its JSON text

    # running it again creates nothing and changes nothing
    before = len(env.frappe.custom_fields)
    env.sync.build_fields()
    assert len(env.frappe.custom_fields) == before
    assert env.sync._backfill(["1_age"]) == 0
