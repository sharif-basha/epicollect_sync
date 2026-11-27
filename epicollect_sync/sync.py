"""Epicollect5 -> Frappe sync. Every question becomes its own field on Epicollect Entry."""
import hashlib
import json
import re
import time
from datetime import timedelta, timezone

import frappe
import requests
from dateutil import parser as dtparser
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import now_datetime

BASE = "https://five.epicollect.net/api"
OVERLAP_MIN = 5
DT = "Epicollect Entry"
# API key -> built-in field. Every other API key becomes a generated "q_..." field (see field_map).
CORE = {"ec5_uuid": "ec5_uuid", "title": "entry_title", "created_at": "created_at",
        "uploaded_at": "uploaded_at", "created_by": "created_by"}
TOKEN_KEY = "epicollect_sync_token"
JOB = dict(queue="long", timeout=3600, job_id="epicollect_sync_run", deduplicate=True)  # one job at a time


@frappe.whitelist()
def sync_now():
    frappe.only_for("System Manager")
    frappe.enqueue("epicollect_sync.sync.run_sync", trigger="Manual", **JOB)


@frappe.whitelist()
def create_fields():
    frappe.only_for("System Manager")
    frappe.enqueue("epicollect_sync.sync.build_fields", **JOB)


def scheduled():
    if frappe.db.get_single_value("Epicollect Settings", "enabled"):
        frappe.enqueue("epicollect_sync.sync.run_sync", trigger="Scheduled", **JOB)


# ---------------------------------------------------------------- Epicollect API
def _dt(v):
    if not v:
        return None
    d = dtparser.parse(str(v))
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _token(s, force=False):
    cache = frappe.cache()  # reuse: Epicollect limits token requests per hour
    if not force and (t := cache.get_value(TOKEN_KEY)):
        return t
    r = requests.post(f"{BASE}/oauth/token", timeout=30, json={
        "grant_type": "client_credentials", "client_id": s.client_id,
        "client_secret": s.get_password("client_secret")})
    if r.status_code == 429:
        wait = r.headers.get("Retry-After")
        raise RuntimeError(
            "Epicollect refused a new access token (HTTP 429): the token rate limit has been reached. "
            f"Wait {wait + ' seconds' if wait else 'about an hour'} and try again. Other apps that use the same "
            "client ID (for example a Render sync page) count towards this limit. Do not retry in a loop.")
    r.raise_for_status()
    d = r.json()
    cache.set_value(TOKEN_KEY, d["access_token"], expires_in_sec=max(60, int(d.get("expires_in", 7200)) - 60))
    return d["access_token"]


def _get(s, path, params=None):
    wait, refreshed = 2, False
    while True:
        r = requests.get(BASE + path, headers={"Authorization": f"Bearer {_token(s)}"}, params=params, timeout=60)
        if r.status_code == 429:  # rate limited: back off and retry
            time.sleep(wait)
            wait = min(wait * 2, 120)
            continue
        if r.status_code == 401 and not refreshed:
            _token(s, force=True)
            refreshed = True
            continue
        r.raise_for_status()
        return r.json()


def _pages(s, since=""):
    params = {"per_page": int(s.per_page or 500), "format": "json", "sort_by": "uploaded_at", "sort_order": "ASC"}
    if s.form_ref:
        params["form_ref"] = s.form_ref
    if s.map_index:
        params["map_index"] = s.map_index
    if since:
        params.update(filter_by="uploaded_at", filter_from=since)
    page = 1
    while True:
        body = _get(s, f"/export/entries/{s.project_slug}", {**params, "page": page})
        yield body.get("data", {}).get("entries", [])
        if page >= (body.get("meta", {}).get("last_page") or page):
            return
        page += 1
        time.sleep(1)


def _labels(s):
    """Best effort: API key -> question text, read from the project definition. Falls back to {}."""
    try:
        d = _get(s, f"/export/project/{s.project_slug}")
    except Exception:
        return {}
    questions, maps = {}, {}

    def walk(o, key=None):
        if isinstance(o, dict):
            if "ref" in o and "question" in o:
                questions[o["ref"]] = o["question"]
            if "map_to" in o and key:
                maps[o["map_to"]] = key  # mapping entries are keyed by the input ref
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)

    walk(d)
    return {m: questions[ref] for m, ref in maps.items() if ref in questions}


# ---------------------------------------------------------------- fields
def _map():
    return json.loads(frappe.db.get_single_value("Epicollect Settings", "field_map") or "{}")


def _fieldname(key, used):
    base = "q_" + re.sub(r"[^a-z0-9_]", "_", key.lower())
    if len(base) > 55:
        base = base[:48] + "_" + hashlib.md5(key.encode()).hexdigest()[:6]
    name, i = base, 2
    while name in used:
        name, i = f"{base[:50]}_{i}", i + 1
    return name


def _add_fields(keys, labels=None):
    """One Small Text custom field per new API key. Small Text avoids MariaDB's row-size limit."""
    labels, fmap = labels or {}, _map()
    meta = frappe.get_meta(DT, cached=False)
    used = set(fmap.values()) | {df.fieldname for df in meta.fields}
    prev, fields = meta.fields[-1].fieldname, []
    for k in keys:
        if k in fmap or k in CORE:
            continue
        fn = _fieldname(k, used)
        used.add(fn)
        fmap[k] = fn
        fields.append({"fieldname": fn, "fieldtype": "Small Text", "label": str(labels.get(k) or k)[:140],
                       "read_only": 1, "insert_after": prev})
        prev = fn
    if fields:
        create_custom_fields({DT: fields}, update=True)
        frappe.db.set_single_value("Epicollect Settings", "field_map", json.dumps(fmap))
        frappe.clear_cache(doctype=DT)
        frappe.db.commit()
    return len(fields)


def build_fields():
    """Scan every entry (keys can appear in only a few) and create all missing fields."""
    s = frappe.get_single("Epicollect Settings")
    keys = {}
    for entries in _pages(s):
        for e in entries:
            keys.update(dict.fromkeys(e))
    n = _add_fields([k for k in keys if k not in CORE], _labels(s))
    frappe.publish_realtime("msgprint", f"Epicollect Sync: created {n} new fields on Epicollect Entry.", user=frappe.session.user)


# ---------------------------------------------------------------- sync
def _val(v):
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def _upsert(e, fmap):
    uuid = e["ec5_uuid"]
    vals = {"entry_title": e.get("title"), "created_by": e.get("created_by"),
            "created_at": _dt(e.get("created_at")), "uploaded_at": _dt(e.get("uploaded_at"))}
    vals.update({fmap[k]: _val(v) for k, v in e.items() if k not in CORE})
    if not frappe.db.exists(DT, uuid):
        frappe.get_doc({"doctype": DT, "ec5_uuid": uuid, **vals}).insert(ignore_permissions=True)
        return "inserted"
    old = frappe.db.get_value(DT, uuid, list(vals), as_dict=True)
    if all(str(old.get(f) or "") == str(v or "") for f, v in vals.items()):
        return "unchanged"
    doc = frappe.get_doc(DT, uuid)
    doc.update(vals)
    doc.save(ignore_permissions=True)
    return "updated"


def run_sync(trigger="Manual"):
    s = frappe.get_single("Epicollect Settings")
    log = frappe.get_doc({"doctype": "Epicollect Sync Log", "started_at": now_datetime(),
                          "status": "Running", "triggered_by": trigger}).insert(ignore_permissions=True)
    frappe.db.commit()
    t0, n, newest = time.time(), {"fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0}, s.last_uploaded_at or ""
    try:
        since = ""
        if newest:
            since = (dtparser.parse(newest) - timedelta(minutes=OVERLAP_MIN)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for entries in _pages(s, since):
            entries = [e for e in entries if e.get("ec5_uuid")]
            fmap = _map()
            missing = [k for k in dict.fromkeys(k for e in entries for k in e) if k not in CORE and k not in fmap]
            if missing:  # new question appeared: create its field before saving the page
                _add_fields(missing)
                fmap = _map()
            for e in entries:
                n["fetched"] += 1
                n[_upsert(e, fmap)] += 1
                newest = max(newest, str(e.get("uploaded_at") or ""))
            frappe.db.set_single_value("Epicollect Settings", "last_uploaded_at", newest)
            frappe.db.commit()  # checkpoint per page, so a failed run resumes where it stopped
        status, err = "Success", ""
    except Exception:
        frappe.db.rollback()
        status, err = "Failed", frappe.get_traceback()
        frappe.log_error(err, "Epicollect Sync failed")
        newest = frappe.db.get_single_value("Epicollect Settings", "last_uploaded_at") or ""
    frappe.db.set_value("Epicollect Sync Log", log.name, {**n, "status": status, "error": err,
                        "checkpoint": newest, "duration": round(time.time() - t0, 1)})
    frappe.db.commit()
