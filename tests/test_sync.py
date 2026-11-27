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
