"""Epicollect5 -> Frappe sync. Each project's forms and branches are synced into their own DocTypes."""
import hashlib
import json
import time
from datetime import timedelta

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from epicollect_sync import api, dashboards, fields, notify, targets

PROJECT = "Epicollect Project"
TARGET = "Epicollect Sync Target"
LOG = "Epicollect Sync Log"
OVERLAP_MIN = 5
JOB_TIMEOUT = 3 * 3600
FULL_CHECK_DAYS = {"Daily": 1, "Weekly": 7}
# A full check only marks entries as deleted if Epicollect still returns at least this share of them,
# so an API glitch that returns too few entries can't flag most of the data as deleted.
MIN_SEEN_SHARE = 0.5


def job_id(project):
    # Sync and set-up of one project share a job id on purpose: both create fields, so they must not overlap.
    return f"epicollect_sync::{project}"


# ---------------------------------------------------------------- entry points
def _enqueue(project, method, msg, **kw):
    frappe.only_for("System Manager")
    frappe.get_doc(PROJECT, project).check_permission("write")
    if is_job_enqueued(job_id(project)):
        return {"queued": False, "message": "A sync or set-up job for this project is already queued or running. "
                                            "Wait for it to finish (see Epicollect Sync Log), then try again."}
    frappe.enqueue(method, project=project, queue="long", timeout=JOB_TIMEOUT, job_id=job_id(project),
                   deduplicate=True, **kw)
    return {"queued": True, "message": msg}


def _limit_message(project):
    wait = api.blocked_for(frappe.get_doc(PROJECT, project))
    if wait:
        return {"queued": False, "message": f"Epicollect's limit on new access tokens is reached. Try again in "
                                            f"about {max(1, wait // 60)} minutes; scheduled syncs resume on their own."}


@frappe.whitelist()
def sync_now(project, full_check=0):
    if msg := _limit_message(project):
        return msg
    full = bool(int(full_check or 0))
    return _enqueue(project, "epicollect_sync.sync.run_sync",
                    "Full check queued. Follow it in Epicollect Sync Log." if full
                    else "Sync queued. Follow it in Epicollect Sync Log.", trigger="Manual", full_check=full)


@frappe.whitelist()
def set_up(project):
    if msg := _limit_message(project):
        return msg
    return _enqueue(project, "epicollect_sync.sync.run_setup",
                    "Set-up queued. You will get a message when it finishes.")


@frappe.whitelist()
def reset_checkpoints(project):
    """Done server-side on the sync targets, which users never edit, so no stale form can overwrite them."""
    frappe.only_for("System Manager")
    if is_job_enqueued(job_id(project)):
        frappe.throw("A sync is queued or running for this project. Wait for it to finish, then reset.")
    for name in frappe.get_all(TARGET, filters={"project": project}, pluck="name"):
        frappe.db.set_value(TARGET, name, "last_uploaded_at", "")
    frappe.db.commit()


@frappe.whitelist()
def test_connection(project):
    frappe.only_for("System Manager")
    p = frappe.get_doc(PROJECT, project)
    d = api.project_definition(p)
    info = (d.get("data") or {}).get("project") or {}
    forms = api.forms(d)
    if info.get("name") and p.project_name != info["name"]:
        p.db_set("project_name", info["name"])
    return {"name": info.get("name") or p.project_slug, "forms": [api.clean(f.get("name")) for f in forms],
            "branches": sum(len(api.branches(f)) for f in forms)}


def scheduled():
    """Runs every 5 minutes; queues each enabled project whose interval has passed since its last run."""
    now = now_datetime()
    for p in frappe.get_all(PROJECT, filters={"enabled": 1}, fields=["name", "sync_every"]):
        last = frappe.db.get_value(LOG, {"project": p.name}, "started_at", order_by="started_at desc")
        every = int(p.sync_every or 60)
        if last and get_datetime(last) > add_to_date(now, minutes=-every + 2):  # 2 min slack for scheduler jitter
            continue
        if api.blocked_for(frappe.get_doc(PROJECT, p.name)):
            continue  # token limit reached: a run now would only fail and could extend the wait
        if not is_job_enqueued(job_id(p.name)):
            frappe.enqueue("epicollect_sync.sync.run_sync", project=p.name, trigger="Scheduled", queue="long",
                           timeout=JOB_TIMEOUT, job_id=job_id(p.name), deduplicate=True)


# ---------------------------------------------------------------- set-up
def _project(name):
    p = frappe.get_doc(PROJECT, name)
    missing = [label for f, label in (("project_slug", "Project Slug"), ("client_id", "Client ID"))
               if not (p.get(f) or "").strip()]
    if not p.get_password("client_secret", raise_exception=False):
        missing.append("Client Secret")
    if missing:
        frappe.throw(f"Epicollect Project {name} is missing: {', '.join(missing)}")
    return p


def _definition(p):
    try:
        return api.project_definition(p)
    except api.TokenLimitReached:
        raise
    except Exception:
        frappe.log_error(title=f"Epicollect: could not read project definition of {p.name}",
                         message=frappe.get_traceback())
        return None


def run_setup(project):
    """Create targets and DocTypes for every form and branch, all question fields and typed columns,
    fill typed columns for entries already synced, and create the dashboard."""
    p = _project(project)
    d = api.project_definition(p)  # required here: set-up without the definition can't name anything
    targets.ensure_targets(p, d)
    frappe.db.commit()
    n_text = n_typed = n_filled = 0
    for t in targets.project_targets(p.name):
        q = targets.questions_for(t, d, p)
        keys = list(q) + list(fields.fmap(t))
        if not q:  # unknown definition shape: fall back to scanning the entries for their keys
            seen = {}
            for entries in api.pages(p, t):
                for e in entries:
                    seen.update(dict.fromkeys(e))
            keys += list(seen)
        a, typed_keys = fields.add_fields(t, list(dict.fromkeys(keys)), q)
        fields.ensure_nullable(t)
        n_text, n_typed = n_text + a, n_typed + len(typed_keys)
        n_filled += fields.backfill(t, typed_keys)
    dashboards.ensure(p)
    frappe.db.commit()
    frappe.publish_realtime("msgprint", f"Epicollect Sync ({p.name}): created {n_text} question fields and typed "
                            f"columns for {n_typed} questions; filled typed values on {n_filled} entries.",
                            user=frappe.session.user)


# ---------------------------------------------------------------- entries
def _val(v):
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def _hash(e):
    return hashlib.sha1(json.dumps(e, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _row(t, project, e, fm, tm, loc_key, h):
    """Column values for one entry. Every mapped field is set, so an answer removed in Epicollect is cleared."""
    vals = {"ec5_uuid": targets.uuid_of(t, e), "project": project, "entry_title": _val(e.get("title")),
            "created_by": (_val(e.get("created_by")) or "")[:140] or None,
            "created_at": api.to_utc(e.get("created_at")), "uploaded_at": api.to_utc(e.get("uploaded_at")),
            "is_deleted": 0, "data_hash": h, "raw_json": json.dumps(e, ensure_ascii=False, default=str)}
    if pk := targets.parent_key(t):
        vals["parent_entry"] = e.get(pk) or None
    vals.update({fn: _val(e.get(k)) for k, fn in fm.items()})
    vals.update(fields.typed_row(tm, loc_key, e.get))
    return vals


def save_page(t, project, entries, fm, tm=None, loc_key=""):
    """Insert new entries in bulk; update only those whose data hash changed (or that come back after being
    marked deleted). Returns counts."""
    dt = t.target_doctype
    by_uuid = {targets.uuid_of(t, e): e for e in entries}  # the last copy wins if the API repeats an entry
    n = {"inserted": 0, "updated": 0, "unchanged": 0}
    if not by_uuid:
        return n
    existing = {r[0]: r[1:] for r in frappe.get_all(dt, filters={"name": ["in", list(by_uuid)]},
                                                    fields=["name", "data_hash", "is_deleted"], as_list=True)}
    new_rows = []
    for uuid, e in by_uuid.items():
        h = _hash(e)
        if uuid in existing:
            old_hash, deleted = existing[uuid]
            if old_hash == h and not deleted:
                n["unchanged"] += 1
                continue
            vals = _row(t, project, e, fm, tm or {}, loc_key, h)
            vals.pop("ec5_uuid")
            frappe.db.set_value(dt, uuid, vals)
            n["updated"] += 1
        else:
            new_rows.append(_row(t, project, e, fm, tm or {}, loc_key, h))
    if new_rows:
        now, user = now_datetime(), frappe.session.user
        cols = list(new_rows[0])
        std = ["name", "owner", "modified_by", "creation", "modified", "docstatus", "idx"]
        frappe.db.bulk_insert(dt, std + cols,
                              [[r["ec5_uuid"], user, user, now, now, 0, 0] + [r[c] for c in cols] for r in new_rows])
        n["inserted"] += len(new_rows)
    return n


def _mark_deleted(t, seen):
    """After a full pass: flag entries Epicollect no longer returns. Returns (count, note)."""
    dt = t.target_doctype
    active = frappe.get_all(dt, filters={"is_deleted": 0}, pluck="name")
    gone = [n for n in active if n not in seen]
    if not gone:
        return 0, ""
    if not seen or len(set(active) & seen) < MIN_SEEN_SHARE * len(active):
        return 0, (f"Full check skipped marking {len(gone)} entries as deleted: Epicollect returned fewer than "
                   f"{int(MIN_SEEN_SHARE * 100)}% of the entries already synced, which looks like an API problem.")
    for i in range(0, len(gone), 500):
        frappe.db.set_value(dt, {"name": ["in", gone[i:i + 500]]}, "is_deleted", 1)
    return len(gone), ""


def _sync_target(p, t, full, defn_cache, counts):
    """Sync one target. Incremental from its checkpoint, or a full pass that also flags deleted entries."""
    dt, checkpoint = t.target_doctype, t.last_uploaded_at or ""
    newest = api.to_utc(checkpoint)
    since = None if full else (newest - timedelta(minutes=OVERLAP_MIN) if newest else None)
    seen = set()
    fields.ensure_nullable(t)  # also repairs DocTypes created before this was handled
    for entries in api.pages(p, t, since):
        entries = [e for e in entries if targets.uuid_of(t, e)]
        fm = fields.fmap(t)
        missing = [k for k in dict.fromkeys(k for e in entries for k in e) if not fields.is_core(k) and k not in fm]
        if missing:  # new question appeared: create its field before saving the page
            if "d" not in defn_cache:
                defn_cache["d"] = _definition(p)
            fields.add_fields(t, missing, targets.questions_for(t, defn_cache["d"], p))
            fm = fields.fmap(t)
        for k, v in save_page(t, p.name, entries, fm, fields.tmap(t), fields.location_key(t)).items():
            counts[k] += v
        counts["fetched"] += len(entries)
        seen.update(targets.uuid_of(t, e) for e in entries)
        times = [x for x in (api.to_utc(e.get("uploaded_at")) for e in entries) if x]
        if times and (newest is None or max(times) > newest):
            newest = max(times)
            checkpoint = api.iso(newest)
            frappe.db.set_value(TARGET, t.name, "last_uploaded_at", checkpoint, update_modified=False)
        frappe.db.commit()  # checkpoint per page, so a failed run resumes where it stopped
    if full:
        counts["deleted"], counts["note"] = _mark_deleted(t, seen)
        frappe.db.set_value(TARGET, t.name, "last_full_check", now_datetime(), update_modified=False)
    frappe.db.set_value(TARGET, t.name, {"entry_count": frappe.db.count(dt, {"is_deleted": 0}),
                                         "last_synced": now_datetime()}, update_modified=False)
    frappe.db.commit()


def _full_check_due(p, t):
    days = FULL_CHECK_DAYS.get(p.check_deletions)
    if not days:
        return False
    return not t.last_full_check or get_datetime(t.last_full_check) <= add_to_date(now_datetime(), days=-days, hours=1)


def _close_stale_logs(project, current):
    """Jobs are de-duplicated per project, so any other 'Running' log of this project belongs to a job that
    was killed (timeout, worker restart) before it could record its result."""
    for name in frappe.get_all(LOG, filters={"project": project, "status": "Running", "name": ["!=", current]},
                               pluck="name"):
        frappe.db.set_value(LOG, name, {"status": "Failed", "error": "The job stopped without finishing "
                                        "(timed out or the worker was restarted). The next run resumed from "
                                        "the last saved checkpoint."})


def run_sync(project, trigger="Manual", full_check=False):
    log = frappe.get_doc({"doctype": LOG, "project": project, "started_at": now_datetime(), "status": "Running",
                          "triggered_by": trigger}).insert(ignore_permissions=True)
    _close_stale_logs(project, log.name)
    frappe.db.commit()
    t0, details, status, err, any_full = time.time(), [], "Success", "", False
    try:
        p = _project(project)
        defn_cache = {}
        if d := _definition(p):  # new forms or branches added in Epicollect get their targets and DocTypes
            defn_cache["d"] = d
            if targets.ensure_targets(p, d):
                dashboards.ensure(p)
            frappe.db.commit()  # keep new targets even if a later target fails and rolls back
        if not targets.project_targets(p.name):
            # First run and the definition could not be read: ask again without hiding the reason (401, 404...).
            d = api.project_definition(p)
            targets.ensure_targets(p, d)
            frappe.db.commit()
        for t in targets.project_targets(p.name):
            counts = {"fetched": 0, "inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0, "note": ""}
            full = bool(full_check) or _full_check_due(p, t)
            any_full = any_full or full
            try:
                _sync_target(p, t, full, defn_cache, counts)
            except api.TokenLimitReached as e:
                # No form can sync without a token: stop here instead of asking again for each form.
                frappe.db.rollback()
                status, err = "Failed", err + str(e)
                counts["note"] = "Not synced: Epicollect token limit reached."
                details.append({"target": t.name, "target_doctype": t.target_doctype, **counts})
                break
            except Exception:
                frappe.db.rollback()
                status = "Failed"
                tb = frappe.get_traceback()
                err += f"--- {t.target_doctype}\n{tb}\n"
                counts["note"] = (counts["note"] + " " if counts["note"] else "") + "Failed: see the error below."
            details.append({"target": t.name, "target_doctype": t.target_doctype, **counts})
    except api.TokenLimitReached as e:
        frappe.db.rollback()
        status, err = "Failed", err + str(e)
    except Exception:
        frappe.db.rollback()
        status, err = "Failed", err + frappe.get_traceback()
    if status == "Failed":
        frappe.log_error(title=f"Epicollect Sync failed: {project}", message=err)
    _finish_log(log.name, status, err, details, any_full, time.time() - t0)
    try:
        notify.after_run(project, log.name, status, err)
    except Exception:
        frappe.log_error(title="Epicollect Sync: notification failed", message=frappe.get_traceback())


def _finish_log(name, status, err, details, full, duration):
    doc = frappe.get_doc(LOG, name)
    totals = {k: sum(d[k] for d in details) for k in ("fetched", "inserted", "updated", "unchanged", "deleted")}
    doc.update({**totals, "status": status, "error": err, "full_check": 1 if full else 0,
                "duration": round(duration, 1)})
    for d in details:
        doc.append("details", d)
    doc.flags.ignore_permissions = True
    doc.save()
    frappe.db.commit()
