"""Epicollect5 API client: access tokens, retries, keyset pagination and project definition parsing."""
import hashlib
import time
from datetime import datetime, timedelta, timezone

import frappe
import requests
from dateutil import parser as dtparser
from frappe.utils import strip_html

BASE = "https://five.epicollect.net/api"
TOKEN_KEY = "epicollect_sync_token"
MAX_PER_PAGE = 1000
MAX_ATTEMPTS = 6        # per HTTP request
MAX_BACKOFF = 300       # seconds
MAX_REQUESTS = 100_000  # safety stop for the page loop
NO_ANSWER = {"group", "branch", "readme"}  # containers and notes: no answer of their own


# ---------------------------------------------------------------- time helpers
def to_utc(v):
    """Parse to naive UTC. Returns None for empty or unparseable values (one bad entry must not stop a run)."""
    if not v:
        return None
    try:
        d = v if isinstance(v, datetime) else dtparser.parse(str(v))
    except (ValueError, OverflowError, TypeError):
        return None
    return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d


def iso(d):
    """Naive UTC datetime -> '2024-01-31T10:20:30.123Z'."""
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------- HTTP
_session = None


def http():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _retry_after(r, default):
    try:
        return min(max(float(r.headers.get("Retry-After")), 1), MAX_BACKOFF)
    except (TypeError, ValueError):
        return default


def token_key(p):
    # Keyed by the credentials, so changing the client ID or secret never reuses an old token.
    ident = hashlib.sha1(f"{p.client_id}:{p.get_password('client_secret')}".encode()).hexdigest()[:16]
    return f"{TOKEN_KEY}:{ident}"


def token(p, force=False):
    cache, key = frappe.cache(), token_key(p)  # reuse: Epicollect limits token requests per hour
    if not force and (t := cache.get_value(key)):
        return t
    r = http().post(f"{BASE}/oauth/token", timeout=30, json={
        "grant_type": "client_credentials", "client_id": p.client_id,
        "client_secret": p.get_password("client_secret")})
    if r.status_code == 429:
        wait = r.headers.get("Retry-After")
        raise RuntimeError(
            "Epicollect refused a new access token (HTTP 429): the token rate limit has been reached. "
            f"Wait {wait + ' seconds' if wait else 'about an hour'} and try again. Other apps that use the same "
            "client ID count towards this limit. Do not retry in a loop.")
    if r.status_code in (400, 401):
        raise RuntimeError("Epicollect rejected the client ID or secret. Check them on the Epicollect Project.")
    r.raise_for_status()
    d = r.json()
    cache.set_value(key, d["access_token"], expires_in_sec=max(60, int(d.get("expires_in", 7200)) - 60))
    return d["access_token"]


def get(p, path, params=None):
    """GET with bounded retries: 429 (honours Retry-After), 5xx, timeouts and dropped connections."""
    wait, refreshed = 2, False
    for attempt in range(1, MAX_ATTEMPTS + 1):
        last = attempt == MAX_ATTEMPTS
        try:
            r = http().get(BASE + path, headers={"Authorization": f"Bearer {token(p)}"},
                           params=params, timeout=60)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(wait)
            wait = min(wait * 2, MAX_BACKOFF)
            continue
        if r.status_code == 401 and not refreshed:
            token(p, force=True)
            refreshed = True
            continue
        if (r.status_code == 429 or r.status_code >= 500) and not last:
            time.sleep(_retry_after(r, wait) if r.status_code == 429 else wait)
            wait = min(wait * 2, MAX_BACKOFF)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Epicollect request {path} failed after {MAX_ATTEMPTS} attempts")


def pages(p, t, since=None):
    """Yield pages of a target's entries in uploaded_at order, using keyset pagination.

    Each request asks for entries uploaded since the newest one already seen (minus 1 s, in case the API
    filters at second precision). Page numbers are only used when a whole page shares one timestamp.
    Unlike plain page numbers, an entry edited during the run moves forward and is fetched again
    instead of shifting the pages and making another entry disappear.
    """
    per_page = min(max(int(p.per_page or 500), 1), MAX_PER_PAGE)
    base = {"per_page": per_page, "format": "json", "sort_by": "uploaded_at", "sort_order": "ASC"}
    if t.form_ref:
        base["form_ref"] = t.form_ref
    if t.branch_ref:
        base["branch_ref"] = t.branch_ref
    if p.map_index:
        base["map_index"] = p.map_index
    cursor, page = since, 1
    for _ in range(MAX_REQUESTS):
        params = {**base, "page": page}
        if cursor:
            params.update(filter_by="uploaded_at", filter_from=iso(cursor - timedelta(seconds=1)))
        body = get(p, f"/export/entries/{p.project_slug}", params)
        entries = (body.get("data") or {}).get("entries") or []
        times = [x for x in (to_utc(e.get("uploaded_at")) for e in entries) if x]
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


# ---------------------------------------------------------------- project definition
def project_definition(p):
    return get(p, f"/export/project/{p.project_slug}")


def clean(q):
    return strip_html(str(q or "")).strip()


def _project(d):
    return (d.get("data") or {}).get("project") or {}


def forms(d):
    return [f for f in _project(d).get("forms") or [] if isinstance(f, dict) and f.get("ref")]


def branches(form):
    """Branch questions of a form (top level or inside a group)."""
    out = []

    def walk(lst):
        for i in lst or []:
            if isinstance(i, dict):
                if i.get("type") == "branch" and i.get("ref"):
                    out.append(i)
                walk(i.get("group"))

    walk(form.get("inputs"))
    return out


def _mapping(d, map_index):
    mappings = [m for m in (d.get("meta") or {}).get("project_mapping") or [] if isinstance(m, dict)]
    found = None
    if str(map_index or "").strip():
        found = next((m for m in mappings if str(m.get("map_index")) == str(map_index).strip()), None)
    return found or next((m for m in mappings if m.get("is_default")), None) or (mappings[0] if mappings else {})


def questions(d, form_ref, branch_ref=None, map_index=None):
    """API key -> {"label", "type", "datetime_format", "order"} for one form, or one branch of a form.

    A form's questions exclude those inside its branches (those come with the branch's own entries).
    """
    form = next((f for f in forms(d) if f["ref"] == form_ref), None)
    if not form:
        return {}
    if branch_ref:
        branch = next((b for b in branches(form) if b["ref"] == branch_ref), None)
        if not branch:
            return {}
        scope_inputs = branch.get("branch")
    else:
        scope_inputs = form.get("inputs")
    inputs, order = {}, []

    def walk_inputs(lst):
        for i in lst or []:
            if isinstance(i, dict) and i.get("ref"):
                inputs[i["ref"]] = i
                order.append(i["ref"])
                walk_inputs(i.get("group"))  # not into branches: those are separate targets

    walk_inputs(scope_inputs)
    fmap = (_mapping(d, map_index).get("forms") or {}).get(form_ref) or {}
    if branch_ref:
        fmap = (fmap.get(branch_ref) or {}).get("branch") or {}
    out = {}

    def walk_map(m):
        for ref, v in (m or {}).items():
            if not isinstance(v, dict):
                continue
            i = inputs.get(ref)
            if i and v.get("map_to") and i.get("type") not in NO_ANSWER:
                out[v["map_to"]] = {"label": clean(i.get("question")), "type": i.get("type"),
                                    "datetime_format": i.get("datetime_format"), "order": order.index(ref)}
            walk_map(v.get("group"))

    walk_map(fmap)
    return dict(sorted(out.items(), key=lambda kv: kv[1]["order"]))


def labels_any(d):
    """Shape-agnostic fallback: API key -> {"label"}, from any ref/question and map_to pairs."""
    qs, maps = {}, {}

    def walk(o, key=None):
        if isinstance(o, dict):
            if "ref" in o and "question" in o:
                qs[o["ref"]] = o["question"]
            if "map_to" in o and key:
                maps[o["map_to"]] = key  # mapping entries are keyed by the input ref
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)

    walk(d)
    return {m: {"label": clean(qs[ref])} for m, ref in maps.items() if ref in qs}
