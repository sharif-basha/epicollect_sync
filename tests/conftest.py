"""Stand-alone tests for epicollect_sync.sync, run with plain pytest (no bench needed).

A small in-memory fake replaces the parts of Frappe the sync uses, and a fake Epicollect API serves entries.
Kept outside the app package so `bench run-tests` never imports the fake.
"""
import json
import sys
import types
from datetime import datetime

import pytest


class FakeDB:
    def __init__(self):
        self.single = {"field_map": "", "last_uploaded_at": "", "enabled": 1}
        self.rows = {"Epicollect Entry": {}, "Epicollect Sync Log": {}}
        self.queries = 0

    def get_single_value(self, dt, f):
        return self.single.get(f)

    def set_single_value(self, dt, f, v):
        self.single[f] = v

    def set_value(self, dt, name, vals, *a, **k):
        self.queries += 1
        self.rows[dt][name].update(vals)

    def bulk_insert(self, dt, fields, values):
        self.queries += 1
        for v in values:
            row = dict(zip(fields, v))
            assert row["name"] not in self.rows[dt], "duplicate insert"
            self.rows[dt][row["name"]] = row

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeSettings:
    def __init__(self, db, **kw):
        self.db, self.vals = db, {"project_slug": "proj", "client_id": "cid", "form_ref": "", "map_index": "",
                                   "per_page": 3, **kw}

    def __getattr__(self, k):
        if k in ("db", "vals"):
            raise AttributeError(k)
        return self.vals.get(k, self.db.single.get(k))

    def get(self, k):
        return getattr(self, k)

    def get_password(self, f, raise_exception=True):
        return "secret"


class FakeMeta:
    def __init__(self, names):
        self.fields = [types.SimpleNamespace(fieldname=n) for n in names]


def make_frappe():
    db = FakeDB()
    frappe = types.ModuleType("frappe")
    frappe.db = db
    frappe.settings = FakeSettings(db)
    frappe.session = types.SimpleNamespace(user="Administrator")
    frappe.custom_fields = []
    frappe.enqueued = []
    frappe.errors = []
    frappe.job_running = False
    cache = {}
    frappe.cache = lambda: types.SimpleNamespace(get_value=cache.get,
                                                 set_value=lambda k, v, expires_in_sec=None: cache.__setitem__(k, v))
    frappe.whitelist = lambda *a, **k: (lambda f: f)
    frappe.only_for = lambda role: None
    frappe.enqueue = lambda method, **kw: frappe.enqueued.append((method, kw))
    frappe.get_single = lambda dt: frappe.settings
    frappe.get_meta = lambda dt, cached=True: FakeMeta(
        ["ec5_uuid", "entry_title", "created_at", "uploaded_at", "created_by", "data_hash", "raw_json"]
        + [f["fieldname"] for f in frappe.custom_fields])
    frappe.clear_cache = lambda **k: None
    frappe.publish_realtime = lambda *a, **k: None
    frappe.get_traceback = lambda: __import__("traceback").format_exc()
    frappe.log_error = lambda *a, **k: frappe.errors.append(a)

    def throw(msg):
        raise Exception(msg)
    frappe.throw = throw

    def get_all(dt, filters=None, fields=None, as_list=False, pluck=None):
        db.queries += 1
        rows = list(db.rows[dt].values())
        for k, cond in (filters or {}).items():
            if isinstance(cond, list) and cond[0] == "in":
                rows = [r for r in rows if r.get(k) in cond[1]]
            elif isinstance(cond, list) and cond[0] == "!=":
                rows = [r for r in rows if r.get(k) != cond[1]]
            else:
                rows = [r for r in rows if r.get(k) == cond]
        if pluck:
            return [r[pluck] for r in rows]
        return [tuple(r.get(f) for f in fields) for r in rows] if as_list else rows
    frappe.get_all = get_all

    counter = {"n": 0}

    def get_doc(d):
        def insert(ignore_permissions=False):
            counter["n"] += 1
            name = f"SYNC-{counter['n']:05d}"
            db.rows[d["doctype"]][name] = {"name": name, **d}
            return types.SimpleNamespace(name=name)
        return types.SimpleNamespace(insert=insert)
    frappe.get_doc = get_doc

    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: datetime(2026, 1, 1)
    utils.strip_html = lambda s: __import__("re").sub(r"<[^>]+>", "", s)
    bg = types.ModuleType("frappe.utils.background_jobs")
    bg.is_job_enqueued = lambda job_id: frappe.job_running
    cf = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    cf.create_custom_fields = lambda d, update=True: frappe.custom_fields.extend(d["Epicollect Entry"])
    mods = {"frappe": frappe, "frappe.utils": utils, "frappe.utils.background_jobs": bg,
            "frappe.custom": types.ModuleType("frappe.custom"),
            "frappe.custom.doctype": types.ModuleType("frappe.custom.doctype"),
            "frappe.custom.doctype.custom_field": types.ModuleType("frappe.custom.doctype.custom_field"),
            "frappe.custom.doctype.custom_field.custom_field": cf}
    return frappe, mods


class FakeAPI:
    """Serves /export/entries with uploaded_at filtering, ASC sort and page numbers, like Epicollect."""

    def __init__(self):
        self.entries = {}
        self.calls = []
        self.on_request = None  # hook to mutate data mid-run
        self.fail = []          # status codes to return before succeeding

    def add(self, uuid, uploaded, **answers):
        self.entries[uuid] = {"ec5_uuid": uuid, "title": uuid, "created_at": uploaded,
                              "uploaded_at": uploaded, "created_by": "a@b.c", **answers}

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.on_request:
            self.on_request(len(self.calls))
        if self.fail:
            return FakeResponse(self.fail.pop(0), {})
        if "/export/project/" in url:
            return FakeResponse(200, {"data": {"project": {"forms": [{"inputs": [
                {"ref": "r1", "question": "<b>Age?</b>"}]}]}}, "meta": {"project_mapping": [
                {"forms": {"f": {"r1": {"map_to": "1_age"}}}}]}})
        rows = sorted(self.entries.values(), key=lambda e: e["uploaded_at"])
        if params.get("filter_from"):
            rows = [e for e in rows if e["uploaded_at"] >= params["filter_from"]]
        per, page = params["per_page"], params["page"]
        last = max(1, -(-len(rows) // per))
        chunk = rows[(page - 1) * per: page * per]
        return FakeResponse(200, {"meta": {"last_page": last, "per_page": per, "current_page": page},
                                  "data": {"entries": [json.loads(json.dumps(e)) for e in chunk]}})

    def post(self, url, json=None, timeout=None):
        return FakeResponse(200, {"access_token": "tok", "expires_in": 7200})


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def env(monkeypatch):
    frappe, mods = make_frappe()
    for k, v in mods.items():
        monkeypatch.setitem(sys.modules, k, v)
    sys.modules.pop("epicollect_sync.sync", None)
    import epicollect_sync.sync as sync
    api = FakeAPI()
    monkeypatch.setattr(sync, "_session", api)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    yield types.SimpleNamespace(sync=sync, frappe=frappe, db=frappe.db, api=api)
    sys.modules.pop("epicollect_sync.sync", None)
