"""Epicollect5 -> Frappe sync. Every question becomes its own field on Epicollect Entry."""
import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

import frappe
import requests
from dateutil import parser as dtparser
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils import now_datetime, strip_html
from frappe.utils.background_jobs import is_job_enqueued

BASE = "https://five.epicollect.net/api"
OVERLAP_MIN = 5
DT = "Epicollect Entry"
SETTINGS = "Epicollect Settings"
LOG = "Epicollect Sync Log"
# API key -> built-in field. Every other API key becomes a generated "q_..." field (see field_map).
CORE = {"ec5_uuid": "ec5_uuid", "title": "entry_title", "created_at": "created_at",
        "uploaded_at": "uploaded_at", "created_by": "created_by"}
TOKEN_KEY = "epicollect_sync_token"
JOB_ID = "epicollect_sync_run"
# Sync and field creation share one job id on purpose: both create custom fields, so they must not overlap.
JOB = dict(queue="long", timeout=3600, job_id=JOB_ID, deduplicate=True)
MAX_PER_PAGE = 1000
MAX_ATTEMPTS = 6        # per HTTP request
MAX_BACKOFF = 300       # seconds
MAX_REQUESTS = 100_000  # safety stop for the page loop


# ---------------------------------------------------------------- entry points
def _enqueue(method, msg, **kw):
    frappe.only_for("System Manager")
    if is_job_enqueued(JOB_ID):
        return {"queued": False, "message": "A sync or field creation job is already queued or running. "
                                            "Wait for it to finish (see Epicollect Sync Log), then try again."}
    frappe.enqueue(method, **kw, **JOB)
    return {"queued": True, "message": msg}


@frappe.whitelist()
def sync_now():
    return _enqueue("epicollect_sync.sync.run_sync", "Sync queued. Follow it in Epicollect Sync Log.",
                    trigger="Manual")


@frappe.whitelist()
def create_fields():
    return _enqueue("epicollect_sync.sync.build_fields",
                    "Field creation queued. You will get a message when it finishes.")


@frappe.whitelist()
def reset_checkpoint():
    """Set server-side, so a stale settings form can't overwrite the field map or the checkpoint."""
    frappe.only_for("System Manager")
    if is_job_enqueued(JOB_ID):
        frappe.throw("A sync is queued or running. Wait for it to finish, then reset the checkpoint.")
    frappe.db.set_single_value(SETTINGS, "last_uploaded_at", "")
    frappe.db.commit()


def scheduled():
    if frappe.db.get_single_value(SETTINGS, "enabled"):
        frappe.enqueue("epicollect_sync.sync.run_sync", trigger="Scheduled", **JOB)


# ---------------------------------------------------------------- time helpers
def _dt(v):
    """Parse to naive UTC. Returns None for empty or unparseable values (one bad entry must not stop a run)."""
    if not v:
        return None
    try:
        d = v if isinstance(v, datetime) else dtparser.parse(str(v))
    except (ValueError, OverflowError, TypeError):
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def _iso(d):
    """Naive UTC datetime -> '2024-01-31T10:20:30.123Z'."""
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- Epicollect API
_session = None


def _http():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _retry_after(r, default):
    try:
        return min(max(float(r.headers.get("Retry-After")), 1), MAX_BACKOFF)
    except (TypeError, ValueError):
        return default


def _token_key(s):
    # Keyed by the credentials, so changing the client ID or secret never reuses an old token.
    ident = hashlib.sha1(f"{s.client_id}:{s.get_password('client_secret')}".encode()).hexdigest()[:16]
    return f"{TOKEN_KEY}:{ident}"


def _token(s, force=False):
    cache, key = frappe.cache(), _token_key(s)  # reuse: Epicollect limits token requests per hour
    if not force and (t := cache.get_value(key)):
        return t
    r = _http().post(f"{BASE}/oauth/token", timeout=30, json={
        "grant_type": "client_credentials", "client_id": s.client_id,
        "client_secret": s.get_password("client_secret")})
    if r.status_code == 429:
        wait = r.headers.get("Retry-After")
        raise RuntimeError(
            "Epicollect refused a new access token (HTTP 429): the token rate limit has been reached. "
            f"Wait {wait + ' seconds' if wait else 'about an hour'} and try again. Other apps that use the same "
            "client ID count towards this limit. Do not retry in a loop.")
    r.raise_for_status()
    d = r.json()
    cache.set_value(key, d["access_token"], expires_in_sec=max(60, int(d.get("expires_in", 7200)) - 60))
    return d["access_token"]


def _get(s, path, params=None):
    """GET with bounded retries: 429 (honours Retry-After), 5xx, timeouts and dropped connections."""
    wait, refreshed = 2, False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            r = _http().get(BASE + path, headers={"Authorization": f"Bearer {_token(s)}"},
                            params=params, timeout=60)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(wait)
            wait = min(wait * 2, MAX_BACKOFF)
            continue
        if r.status_code == 401 and not refreshed:
            _token(s, force=True)
            refreshed = True
            continue
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            time.sleep(_retry_after(r, wait) if r.status_code == 429 else wait)
            wait = min(wait * 2, MAX_BACKOFF)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Epicollect request {path} failed after {MAX_ATTEMPTS} attempts")


def _pages(s, since=None):
    """Yield pages of entries in uploaded_at order, using keyset pagination.

    Each request asks for entries uploaded since the newest one already seen (minus 1 s, in case the API
    filters at second precision). Page numbers are only used when a whole page shares one timestamp.
    Unlike plain page numbers, an entry edited during the run moves forward and is fetched again
    instead of shifting the pages and making another entry disappear.
    """
    per_page = min(max(int(s.per_page or 500), 1), MAX_PER_PAGE)
    base = {"per_page": per_page, "format": "json", "sort_by": "uploaded_at", "sort_order": "ASC"}
    if s.form_ref:
        base["form_ref"] = s.form_ref
    if s.map_index:
        base["map_index"] = s.map_index
    cursor, page = since, 1
    for _ in range(MAX_REQUESTS):
        params = {**base, "page": page}
        if cursor:
            params.update(filter_by="uploaded_at", filter_from=_iso(cursor - timedelta(seconds=1)))
        body = _get(s, f"/export/entries/{s.project_slug}", params)
        entries = (body.get("data") or {}).get("entries") or []
        times = [t for t in (_dt(e.get("uploaded_at")) for e in entries) if t]
        if any(b < a for a, b in zip(times, times[1:])):
            # The checkpoint is only safe if results really are sorted by uploaded_at.
            raise RuntimeError("Epicollect did not return entries sorted by uploaded_at, so the sync stopped "
                               "without moving the checkpoint past entries it has not fetched.")
        yield entries
        meta = body.get("meta") or {}
        if not entries:
            return
        if meta.get("last_page") is not None:
            if page >= int(meta["last_page"]):
                return
        elif len(entries) < int(meta.get("per_page") or per_page):
            return
        newest = times[-1] if times else None
        if newest and (cursor is None or newest > cursor):
            cursor, page = newest, 1
        else:  # whole page shares the cursor's timestamp: step through it by page number
            page += 1
        time.sleep(1)
    raise RuntimeError("Epicollect Sync stopped: too many requests in one run")


def _clean(q):
    return strip_html(str(q or "")).strip()


def _definition(s):
    """Best effort: API key -> {"label", "type", "datetime_format"}, read from the project definition.

    Uses the configured form (or the first one) and mapping (or the default one). Questions inside a
    branch are skipped: their answers are not part of the form's entries. Falls back to labels only
    (no types) if the definition has an unexpected shape, and to {} if it can't be fetched.
    """
    try:
        d = _get(s, f"/export/project/{s.project_slug}")
    except Exception:
        return {}
    try:
        out = _parse_definition(d, s.form_ref, s.map_index)
    except Exception:
        out = {}
    return out or {k: {"label": v} for k, v in _labels_any(d).items()}


NO_ANSWER = {"group", "branch", "readme"}  # containers and notes: no answer in the form's entries


def _parse_definition(d, form_ref=None, map_index=None):
    forms = ((d.get("data") or {}).get("project") or {}).get("forms") or []
    form = next((f for f in forms if f.get("ref") == form_ref), None) if form_ref else (forms[0] if forms else None)
    if not form:
        return {}
    inputs = {}

    def walk_inputs(lst, in_branch):
        for i in lst or []:
            if isinstance(i, dict) and i.get("ref"):
                inputs[i["ref"]] = (i, in_branch)
                walk_inputs(i.get("group"), in_branch)
                walk_inputs(i.get("branch"), True)

    walk_inputs(form.get("inputs"), False)
    mappings = [m for m in (d.get("meta") or {}).get("project_mapping") or [] if isinstance(m, dict)]
    mapping = None
    if str(map_index or "").strip():
        mapping = next((m for m in mappings if str(m.get("map_index")) == str(map_index).strip()), None)
    mapping = mapping or next((m for m in mappings if m.get("is_default")), None) or (mappings[0] if mappings else {})
    out = {}

    def walk_map(m):
        for ref, v in (m or {}).items():
            if not isinstance(v, dict):
                continue
            i, in_branch = inputs.get(ref, (None, True))
            if i and not in_branch and v.get("map_to") and i.get("type") not in NO_ANSWER:
                out[v["map_to"]] = {"label": _clean(i.get("question")), "type": i.get("type"),
                                    "datetime_format": i.get("datetime_format")}
            walk_map(v.get("group"))

    walk_map((mapping.get("forms") or {}).get(form["ref"]))
    return out


def _labels_any(d):
    """Shape-agnostic fallback: API key -> question text, from any ref/question and map_to pairs."""
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
    return {m: _clean(questions[ref]) for m, ref in maps.items() if ref in questions}


# ---------------------------------------------------------------- fields
def _map():
    return json.loads(frappe.db.get_single_value(SETTINGS, "field_map") or "{}")


def _fieldname(key, used):
    base = "q_" + re.sub(r"[^a-z0-9_]", "_", key.lower())
    if len(base) > 55:
        base = base[:48] + "_" + hashlib.md5(key.encode()).hexdigest()[:6]
    name, i = base, 2
    while name in used:
        name, i = f"{base[:50]}_{i}", i + 1
    return name


# Typed companion columns. The q_ Small Text field always keeps the answer exactly as received; these
# hold a parsed copy for filtering, sorting, charts and maps. A value that can't be parsed is left empty.
KIND = {"integer": "int", "decimal": "number", "date": "date", "location": "location"}
PARTS = {  # kind -> (part, fieldname suffix, fieldtype, label suffix, extra field props)
    "int": [("value", "_num", "Int", "number", {})],
    "number": [("value", "_num", "Float", "number", {"precision": "6"})],
    "date": [("value", "_date", "Date", "date", {})],
    "location": [("lat", "_lat", "Float", "latitude", {"precision": "7"}),
                 ("lng", "_lng", "Float", "longitude", {"precision": "7"}),
                 ("acc", "_acc", "Float", "accuracy m", {"precision": "1"})],
}
INT_MIN, INT_MAX = -(2 ** 31), 2 ** 31 - 1  # Frappe Int is a signed 32-bit column


def _tmap():
    return json.loads(frappe.db.get_single_value(SETTINGS, "typed_map") or "{}")


def _unique(name, used):
    out, i = name, 2
    while out in used:
        out, i = f"{name[:58]}_{i}", i + 1
    used.add(out)
    return out


def _add_fields(keys, defn=None):
    """Create missing fields for these API keys: one Small Text field per key (Small Text avoids MariaDB's
    row-size limit), plus typed companion columns for number, date and location questions.
    Returns (text fields created, keys that got new typed columns)."""
    defn, fmap, tmap = defn or {}, _map(), _tmap()
    meta = frappe.get_meta(DT, cached=False)
    used = set(fmap.values()) | {df.fieldname for df in meta.fields}
    prev, fields, n_text, typed_keys = meta.fields[-1].fieldname, [], 0, []
    for k in keys:
        if k in CORE:
            continue
        info = defn.get(k) or {}
        label = str(info.get("label") or k)
        if k not in fmap:
            fmap[k] = fn = _fieldname(k, used)
            used.add(fn)
            fields.append({"fieldname": fn, "fieldtype": "Small Text", "label": label[:140],
                           "read_only": 1, "insert_after": prev})
            prev, n_text = fn, n_text + 1
        kind = KIND.get(info.get("type"))
        if kind and k not in tmap:
            after, parts = fmap[k], {}
            for part, suffix, ftype, lsuffix, extra in PARTS[kind]:
                tfn = _unique(fmap[k][:64 - len(suffix)] + suffix, used)
                fields.append({"fieldname": tfn, "fieldtype": ftype, "label": f"{label[:120]} ({lsuffix})",
                               "read_only": 1, "insert_after": after, **extra})
                parts[part], after = tfn, tfn
            if prev == fmap[k]:
                prev = after
            tmap[k] = {"kind": kind, "fields": parts,
                       "dayfirst": str(info.get("datetime_format") or "").lower().startswith("dd")}
            typed_keys.append(k)
    if fields:
        create_custom_fields({DT: fields}, update=True)
        frappe.db.set_single_value(SETTINGS, "field_map", json.dumps(fmap))
        frappe.db.set_single_value(SETTINGS, "typed_map", json.dumps(tmap))
        frappe.clear_cache(doctype=DT)
        frappe.db.commit()
    return n_text, typed_keys


def _num(v, kind):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).strip())
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if kind == "int":
        return int(f) if f.is_integer() and INT_MIN <= f <= INT_MAX else None
    return f


ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def _date(v, dayfirst=False):
    """Epicollect stores dates as ISO text at local midnight ('2024-01-31T00:00:00.000'); use the date part
    as is, with no timezone shift. Anything else is parsed with the question's day/month order."""
    if not v or not isinstance(v, str):
        return None
    if m := ISO_DATE.match(v):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    try:
        return dtparser.parse(v, dayfirst=dayfirst).date()
    except (ValueError, OverflowError):
        return None


def _location(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    if not isinstance(v, dict):
        return {}
    lat, lng = _num(v.get("latitude"), "number"), _num(v.get("longitude"), "number")
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {}
    acc = _num(v.get("accuracy"), "number")
    return {"lat": lat, "lng": lng, "acc": acc if acc is not None and acc >= 0 else None}


def _typed(spec, v):
    """Typed column values for one answer: {fieldname: value}. Empty or unparseable answers give None."""
    kind, f = spec["kind"], spec["fields"]
    if kind == "location":
        loc = _location(v)
        return {fn: loc.get(part) for part, fn in f.items()}
    if kind == "date":
        return {f["value"]: _date(v, spec.get("dayfirst"))}
    return {f["value"]: _num(v, kind)}


def _backfill(keys):
    """Fill typed columns of existing entries from their text fields. Returns the number of entries changed."""
    fmap, tmap = _map(), _tmap()
    specs = {k: tmap[k] for k in keys if k in tmap and k in fmap}
    if not specs:
        return 0
    cols = list(dict.fromkeys([fmap[k] for k in specs] + [fn for sp in specs.values() for fn in sp["fields"].values()]))
    last, changed = "", 0
    while True:
        rows = frappe.get_all(DT, filters={"name": [">", last]}, fields=["name"] + cols,
                              order_by="name asc", limit_page_length=1000)
        if not rows:
            return changed
        for r in rows:
            vals = {}
            for k, spec in specs.items():
                vals.update(_typed(spec, r.get(fmap[k])))
            vals = {fn: v for fn, v in vals.items() if r.get(fn) != v}
            if vals:
                frappe.db.set_value(DT, r["name"], vals, update_modified=False)
                changed += 1
        last = rows[-1]["name"]
        frappe.db.commit()


def build_fields():
    """Scan every entry (keys can appear in only a few) and create all missing fields, including typed
    columns for questions synced before they existed, then fill those columns for existing entries."""
    s = _settings()
    defn = _definition(s)
    keys = dict.fromkeys(defn)
    for entries in _pages(s):
        for e in entries:
            keys.update(dict.fromkeys(e))
    keys.update(dict.fromkeys(_map()))
    n_text, typed_keys = _add_fields([k for k in keys if k not in CORE], defn)
    n_filled = _backfill(typed_keys)
    frappe.publish_realtime("msgprint", f"Epicollect Sync: created {n_text} question fields and typed columns "
                            f"for {len(typed_keys)} questions; filled typed values on {n_filled} entries.",
                            user=frappe.session.user)


# ---------------------------------------------------------------- sync
def _settings():
    s = frappe.get_single(SETTINGS)
    missing = [label for f, label in (("project_slug", "Project Slug"), ("client_id", "Client ID"))
               if not (s.get(f) or "").strip()]
    if not s.get_password("client_secret", raise_exception=False):
        missing.append("Client Secret")
    if missing:
        frappe.throw(f"Epicollect Settings is missing: {', '.join(missing)}")
    return s


def _val(v):
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def _hash(e):
    return hashlib.sha1(json.dumps(e, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _row(e, fmap, h, tmap=None):
    """Column values for one entry. Every mapped field is set, so an answer removed in Epicollect is cleared."""
    vals = {"ec5_uuid": e["ec5_uuid"], "entry_title": _val(e.get("title")),
            "created_by": (_val(e.get("created_by")) or "")[:140] or None,
            "created_at": _dt(e.get("created_at")), "uploaded_at": _dt(e.get("uploaded_at")),
            "data_hash": h, "raw_json": json.dumps(e, ensure_ascii=False, default=str)}
    vals.update({fn: _val(e.get(k)) for k, fn in fmap.items()})
    for k, spec in (tmap or {}).items():
        vals.update(_typed(spec, e.get(k)))
    return vals


def _save_page(entries, fmap, tmap=None):
    """Insert new entries in bulk; update only those whose data hash changed. Returns counts."""
    by_uuid = {e["ec5_uuid"]: e for e in entries}  # the last copy wins if the API repeats an entry
    n = {"inserted": 0, "updated": 0, "unchanged": 0}
    if not by_uuid:
        return n
    existing = dict(frappe.get_all(DT, filters={"name": ["in", list(by_uuid)]},
                                   fields=["name", "data_hash"], as_list=True))
    new_rows = []
    for uuid, e in by_uuid.items():
        h = _hash(e)
        if uuid in existing:
            if existing[uuid] == h:
                n["unchanged"] += 1
                continue
            vals = _row(e, fmap, h, tmap)
            vals.pop("ec5_uuid")
            frappe.db.set_value(DT, uuid, vals)
            n["updated"] += 1
        else:
            new_rows.append(_row(e, fmap, h, tmap))
    if new_rows:
        now, user = now_datetime(), frappe.session.user
        cols = list(new_rows[0])
        std = ["name", "owner", "modified_by", "creation", "modified", "docstatus", "idx"]
        frappe.db.bulk_insert(DT, std + cols,
                              [[r["ec5_uuid"], user, user, now, now, 0, 0] + [r[c] for c in cols] for r in new_rows])
        n["inserted"] += len(new_rows)
    return n


def _close_stale_logs(current):
    """Jobs are de-duplicated, so any other 'Running' log belongs to a job that was killed (timeout, worker
    restart) before it could record its result."""
    for name in frappe.get_all(LOG, filters={"status": "Running", "name": ["!=", current]}, pluck="name"):
        frappe.db.set_value(LOG, name, {"status": "Failed", "error": "The job stopped without finishing "
                                        "(timed out or the worker was restarted). The next run resumed from "
                                        "the last saved checkpoint."})


def run_sync(trigger="Manual"):
    log = frappe.get_doc({"doctype": LOG, "started_at": now_datetime(),
                          "status": "Running", "triggered_by": trigger}).insert(ignore_permissions=True)
    _close_stale_logs(log.name)
    frappe.db.commit()
    t0, n = time.time(), {"fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0}
    checkpoint = ""
    try:
        s = _settings()
        checkpoint = s.last_uploaded_at or ""
        newest = _dt(checkpoint)
        since = newest - timedelta(minutes=OVERLAP_MIN) if newest else None
        for entries in _pages(s, since):
            entries = [e for e in entries if e.get("ec5_uuid")]
            fmap = _map()
            missing = [k for k in dict.fromkeys(k for e in entries for k in e) if k not in CORE and k not in fmap]
            if missing:  # new question appeared: create its field before saving the page
                _add_fields(missing, _definition(s))
                fmap = _map()
            for k, v in _save_page(entries, fmap, _tmap()).items():
                n[k] += v
            n["fetched"] += len(entries)
            times = [t for t in (_dt(e.get("uploaded_at")) for e in entries) if t]
            if times and (newest is None or max(times) > newest):
                newest = max(times)
                checkpoint = _iso(newest)
                frappe.db.set_single_value(SETTINGS, "last_uploaded_at", checkpoint)
            frappe.db.commit()  # checkpoint per page, so a failed run resumes where it stopped
        status, err = "Success", ""
    except Exception:
        frappe.db.rollback()
        status, err = "Failed", frappe.get_traceback()
        frappe.log_error(err, "Epicollect Sync failed")
        checkpoint = frappe.db.get_single_value(SETTINGS, "last_uploaded_at") or ""
    frappe.db.set_value(LOG, log.name, {**n, "status": status, "error": err,
                        "checkpoint": checkpoint, "duration": round(time.time() - t0, 1)})
    frappe.db.commit()
