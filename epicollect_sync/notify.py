"""Emails for sync failures, recoveries and field inactivity. Recipients come from the Epicollect Project."""
import re

import frappe
from frappe.utils import add_days, get_url_to_form, now_datetime, validate_email_address

PROJECT = "Epicollect Project"
LOG = "Epicollect Sync Log"


def recipients(p):
    return [e for e in re.split(r"[\s,;]+", p.notify_emails or "") if e and validate_email_address(e)]


def _send(p, subject, message):
    to = recipients(p)
    if not to:
        return
    try:
        frappe.sendmail(recipients=to, subject=subject, message=message, reference_doctype=PROJECT,
                        reference_name=p.name)
    except frappe.OutgoingEmailError:
        frappe.log_error(title="Epicollect Sync: alert not sent",
                         message=f"{subject}\n\nNo outgoing Email Account is set up, so this alert was not emailed.")


def after_run(project, log_name, status, err):
    """Email on the first failure after a success, and when a failing project recovers. Not on every failed
    run, so an outage of several hours sends two emails, not one per run."""
    previous = frappe.get_all(LOG, filters={"project": project, "name": ["!=", log_name],
                                            "status": ["in", ["Success", "Failed"]]},
                              fields=["status"], order_by="started_at desc", limit=1)
    prev = previous[0].status if previous else None
    p = frappe.get_doc(PROJECT, project)
    link = get_url_to_form(LOG, log_name)
    if status == "Failed" and prev != "Failed":
        last_line = next((ln for ln in reversed((err or "").strip().splitlines()) if ln.strip()), "")
        _send(p, f"Epicollect sync failed: {p.name}",
              f"<p>The Epicollect sync for <b>{frappe.utils.escape_html(p.name)}</b> failed.</p>"
              f"<p><code>{frappe.utils.escape_html(last_line[:500])}</code></p>"
              f"<p><a href='{link}'>Open the sync log</a>. You will get another email when it works again.</p>")
    elif status == "Success" and prev == "Failed":
        _send(p, f"Epicollect sync working again: {p.name}",
              f"<p>The Epicollect sync for <b>{frappe.utils.escape_html(p.name)}</b> is working again.</p>"
              f"<p><a href='{link}'>Open the sync log</a>.</p>")


def check_inactivity():
    """Daily: warn when an enabled project has received no new entries for its configured number of days."""
    for p in frappe.get_all(PROJECT, filters={"enabled": 1, "inactivity_days": [">", 0]},
                            fields=["name", "inactivity_days"]):
        since = add_days(now_datetime(), -int(p.inactivity_days))
        recent = frappe.db.exists(LOG, {"project": p.name, "inserted": [">", 0], "started_at": [">=", since]})
        synced = frappe.db.exists(LOG, {"project": p.name, "status": "Success", "started_at": [">=", since]})
        if not recent and synced:  # only when syncs work: a broken sync already sent its own email
            doc = frappe.get_doc(PROJECT, p.name)
            _send(doc, f"No new Epicollect entries for {p.inactivity_days} days: {p.name}",
                  f"<p>No new entries have arrived for <b>{frappe.utils.escape_html(p.name)}</b> in the last "
                  f"{p.inactivity_days} days, although syncs are working. Check that field teams are collecting "
                  f"and uploading data.</p><p><a href='{get_url_to_form(PROJECT, p.name)}'>Open the project</a>.</p>")
