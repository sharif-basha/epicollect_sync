"""Move version 0.1's single Epicollect Settings into an Epicollect Project with one sync target that keeps
using the existing Epicollect Entry DocType, its fields, field maps and checkpoint."""
import frappe
from frappe.utils.password import get_decrypted_password


def execute():
	vals = dict(frappe.db.sql("select field, value from tabSingles where doctype='Epicollect Settings'"))
	slug = (vals.get("project_slug") or "").strip()
	if slug and not frappe.db.exists("Epicollect Project", slug):
		secret = get_decrypted_password("Epicollect Settings", "Epicollect Settings", "client_secret",
										raise_exception=False)
		p = frappe.get_doc({
			"doctype": "Epicollect Project", "project_slug": slug, "enabled": int(vals.get("enabled") or 0),
			"client_id": vals.get("client_id"), "client_secret": secret, "map_index": vals.get("map_index"),
			"per_page": int(vals.get("per_page") or 500), "sync_every": "60", "check_deletions": "Weekly",
		})
		p.flags.ignore_mandatory = True  # an incomplete old setup still moves over; it fails clearly at sync time
		p.insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Epicollect Sync Target", "project": p.name, "kind": "Form",
			"target_doctype": "Epicollect Entry",
			"title_label": "Epicollect Entry", "form_ref": vals.get("form_ref") or "",
			"last_uploaded_at": vals.get("last_uploaded_at") or "", "field_map": vals.get("field_map") or "{}",
			"typed_map": vals.get("typed_map") or "{}", "location_key": _first_location(vals.get("typed_map")),
		}).insert(ignore_permissions=True)
		frappe.db.sql("update `tabEpicollect Entry` set project=%s where ifnull(project, '')=''", p.name)
		frappe.db.sql("update `tabEpicollect Sync Log` set project=%s where ifnull(project, '')=''", p.name)
	frappe.db.delete("Singles", {"doctype": "Epicollect Settings"})
	frappe.db.delete("__Auth", {"doctype": "Epicollect Settings"})
	if frappe.db.exists("DocType", "Epicollect Settings"):
		frappe.delete_doc("DocType", "Epicollect Settings", force=True, ignore_permissions=True)


def _first_location(typed_map):
	import json
	tm = json.loads(typed_map or "{}")
	return next((k for k, spec in tm.items() if spec.get("kind") == "location"), "")
