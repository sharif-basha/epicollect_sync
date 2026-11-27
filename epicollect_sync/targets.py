"""Sync targets: one per form and one per branch question of an Epicollect project, each with its own DocType."""
import re

import frappe

from epicollect_sync import api

PROJECT = "Epicollect Project"
TARGET = "Epicollect Sync Target"
LEGACY_DT = "Epicollect Entry"
MODULE = "Epicollect Sync"
VIEWER = "Epicollect Viewer"
MAX_DT_NAME = 61  # table name "tab<name>" must fit MariaDB's 64 characters


def ensure_role():
    if not frappe.db.exists("Role", VIEWER):
        frappe.get_doc({"doctype": "Role", "role_name": VIEWER, "desk_access": 1}).insert(ignore_permissions=True)


def project_targets(project):
    """Targets in sync order: forms before their children, each form's branches after it."""
    return [frappe.get_doc(TARGET, n) for n in frappe.get_all(
        TARGET, filters={"project": project}, order_by="sort_order asc, creation asc", pluck="name")]


def _ascii_words(s):
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]+", " ", str(s or ""))).strip()


def prefix(p):
    return _ascii_words(p.doctype_prefix) or _ascii_words(p.project_slug.replace("-", " ")).title() or "Epicollect"


def _doctype_name(base, fallback):
    words = _ascii_words(base) or fallback
    if not words[0].isalpha():
        words = "EC " + words
    name, i = words[:MAX_DT_NAME].strip(), 2
    while frappe.db.exists("DocType", name):
        suffix = f" {i}"
        name, i = words[:MAX_DT_NAME - len(suffix)].strip() + suffix, i + 1
    return name


def _core_fields(parent_dt=None, parent_label=None):
    fields = [
        {"fieldname": "ec5_uuid", "fieldtype": "Data", "label": "Entry UUID", "reqd": 1, "unique": 1, "read_only": 1},
        {"fieldname": "project", "fieldtype": "Link", "options": PROJECT, "label": "Project", "read_only": 1},
    ]
    if parent_dt:
        fields.append({"fieldname": "parent_entry", "fieldtype": "Link", "options": parent_dt,
                       "label": parent_label or "Parent Entry", "read_only": 1, "search_index": 1,
                       "in_standard_filter": 1})
    fields += [
        {"fieldname": "entry_title", "fieldtype": "Small Text", "label": "Title", "read_only": 1, "in_list_view": 1},
        {"fieldname": "created_at", "fieldtype": "Datetime", "label": "Created At (UTC)", "read_only": 1},
        {"fieldname": "uploaded_at", "fieldtype": "Datetime", "label": "Uploaded At (UTC)", "read_only": 1,
         "in_list_view": 1, "in_standard_filter": 1, "search_index": 1},
        {"fieldname": "created_by", "fieldtype": "Data", "label": "Created By", "read_only": 1,
         "in_list_view": 1, "in_standard_filter": 1, "search_index": 1},
        {"fieldname": "is_deleted", "fieldtype": "Check", "label": "Deleted in Epicollect", "read_only": 1,
         "in_standard_filter": 1, "search_index": 1},
        {"fieldname": "latitude", "fieldtype": "Float", "label": "Latitude", "precision": "7", "read_only": 1},
        {"fieldname": "longitude", "fieldtype": "Float", "label": "Longitude", "precision": "7", "read_only": 1},
        {"fieldname": "sync_section", "fieldtype": "Section Break", "label": "Sync", "collapsible": 1},
        {"fieldname": "data_hash", "fieldtype": "Data", "label": "Data Hash", "read_only": 1, "hidden": 1},
        {"fieldname": "raw_json", "fieldtype": "Code", "options": "JSON", "label": "Raw Entry (JSON)", "read_only": 1},
        {"fieldname": "answers_section", "fieldtype": "Section Break", "label": "Answers"},
    ]
    return fields


def create_doctype(name, parent_dt=None, parent_label=None):
    ensure_role()
    doc = frappe.get_doc({
        "doctype": "DocType", "name": name, "module": MODULE, "custom": 1,
        "autoname": "field:ec5_uuid", "naming_rule": "By fieldname", "title_field": "entry_title",
        "search_fields": "entry_title,created_by", "sort_field": "uploaded_at", "sort_order": "DESC",
        "fields": _core_fields(parent_dt, parent_label),
        "permissions": [
            {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "report": 1, "export": 1,
             "print": 1, "email": 1, "share": 1},
            {"role": VIEWER, "read": 1, "report": 1, "export": 1, "print": 1},
        ],
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def ensure_targets(p, d):
    """Create a target (and its DocType) for every form and branch in the project definition.
    Existing targets are kept; a legacy target with no form ref is matched to the first form."""
    existing = {(t.form_ref or "", t.branch_ref or ""): t for t in project_targets(p.name)}
    forms = api.forms(d)
    if forms and ("", "") in existing and (forms[0]["ref"], "") not in existing:
        legacy = existing.pop(("", ""))
        legacy.db_set({"form_ref": forms[0]["ref"], "form_name": api.clean(forms[0].get("name"))})
        existing[(forms[0]["ref"], "")] = legacy
    created, order, parent = [], 0, None
    for fi, form in enumerate(forms):
        form_name = api.clean(form.get("name")) or f"Form {fi + 1}"
        dt_form = _ascii_words(form_name) or f"Form {fi + 1}"  # DocType names must be ASCII
        t = existing.get((form["ref"], ""))
        if not t:
            parent_dt = parent.target_doctype if parent else None
            dt = create_doctype(_doctype_name(f"{prefix(p)} {dt_form}", f"{prefix(p)} Form {fi + 1}"),
                                parent_dt, parent.title_label if parent else None)
            t = _new_target(p, "Form", dt, form_name, form["ref"], parent=parent)
            created.append(t)
        _set_order(t, order)
        order += 1
        for bi, br in enumerate(api.branches(form)):
            label = api.clean(br.get("question")) or f"Branch {bi + 1}"
            b = existing.get((form["ref"], br["ref"]))
            if not b:
                dt_label = _ascii_words(label)[:40] or f"Branch {bi + 1}"
                name = _doctype_name(f"{t.target_doctype} {dt_label}", f"{t.target_doctype} Branch {bi + 1}")
                dt = create_doctype(name, t.target_doctype, t.title_label)
                b = _new_target(p, "Branch", dt, form_name, form["ref"], br["ref"], label, parent=t)
                created.append(b)
            _set_order(b, order)
            order += 1
        parent = t
    return created


def _set_order(t, order):
    if t.sort_order != order:
        t.db_set("sort_order", order, update_modified=False)


def _new_target(p, kind, dt, form_name, form_ref, branch_ref=None, branch_label=None, parent=None):
    return frappe.get_doc({
        "doctype": TARGET, "project": p.name, "kind": kind, "target_doctype": dt,
        "title_label": dt, "form_ref": form_ref, "form_name": form_name, "branch_ref": branch_ref or "",
        "branch_label": branch_label or "", "parent_target": parent.name if parent else None,
    }).insert(ignore_permissions=True)


def parent_key(t):
    """Entry key that links to the parent: child form -> parent form entry, branch -> owning entry."""
    if t.kind == "Branch":
        return "ec5_branch_owner_uuid"
    return "ec5_parent_uuid" if t.parent_target else ""


def uuid_of(t, e):
    return e.get("ec5_branch_uuid") or e.get("ec5_uuid") if t.kind == "Branch" else e.get("ec5_uuid")


def questions_for(t, d, p):
    """Question definitions for one target, or labels only if the definition has an unexpected shape."""
    if not d:
        return {}
    try:
        q = api.questions(d, t.form_ref, t.branch_ref or None, p.map_index)
    except Exception:
        q = {}
    return q or (api.labels_any(d) if t.kind == "Form" and not t.parent_target else {})
