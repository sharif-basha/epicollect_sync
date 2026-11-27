"""Per-project dashboard: charts and number cards for each synced form and branch.

Created once per target and then left alone, so people can rearrange or edit them in the desk.
"""
import json

import frappe

from epicollect_sync import targets


def _name(dt, what):
    return f"{dt[:100]}: {what}"


def _chart(dt, what, **kw):
    name = _name(dt, what)
    if not frappe.db.exists("Dashboard Chart", name):
        frappe.get_doc({"doctype": "Dashboard Chart", "chart_name": name, "document_type": dt, "is_public": 1,
                        "filters_json": json.dumps([[dt, "is_deleted", "=", 0, False]]), **kw}
                       ).insert(ignore_permissions=True)
    return name


def _card(dt, what, **kw):
    name = _name(dt, what)
    if not frappe.db.exists("Number Card", name):
        frappe.get_doc({"doctype": "Number Card", "label": name, "document_type": dt, "is_public": 1,
                        "function": "Count", "type": "Document Type", **kw}).insert(ignore_permissions=True)
    return name


def ensure(p):
    """Create the charts and cards for every target and a Dashboard named after the project."""
    charts, cards = [], []
    for t in targets.project_targets(p.name):
        dt = t.target_doctype
        active = [[dt, "is_deleted", "=", 0, False]]
        cards.append(_card(dt, "Entries", filters_json=json.dumps(active),
                           show_percentage_stats=1, stats_time_interval="Weekly"))
        cards.append(_card(dt, "Last 7 days", filters_json=json.dumps(active), dynamic_filters_json=json.dumps(
            [[dt, "uploaded_at", ">=", "frappe.datetime.add_days(frappe.datetime.now_date(), -7)"]])))
        charts.append(_chart(dt, "Entries per day", chart_type="Count", type="Line", based_on="uploaded_at",
                             timeseries=1, timespan="Last Quarter", time_interval="Daily"))
        if t.kind == "Form" and not t.parent_target:
            charts.append(_chart(dt, "Entries by collector", chart_type="Group By", type="Bar",
                                 group_by_type="Count", group_by_based_on="created_by", number_of_groups=15))
    name = f"Epicollect {p.name}"[:140]
    if frappe.db.exists("Dashboard", name):
        d = frappe.get_doc("Dashboard", name)
    else:
        d = frappe.get_doc({"doctype": "Dashboard", "dashboard_name": name})
    have_charts = {c.chart for c in d.charts}
    have_cards = {c.card for c in d.cards}
    for c in charts:
        if c not in have_charts:
            d.append("charts", {"chart": c, "width": "Full" if c.endswith("per day") else "Half"})
    for c in cards:
        if c not in have_cards:
            d.append("cards", {"card": c})
    d.flags.ignore_permissions = True
    d.save()
    return d.name
