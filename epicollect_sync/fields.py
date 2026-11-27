"""Question fields on a target DocType: names, typed companion columns, value conversion and backfill."""
import hashlib
import json
import re
from datetime import date

import frappe
from dateutil import parser as dtparser
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

TARGET = "Epicollect Sync Target"
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
ISO_DATE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def is_core(key):
    """Entry metadata, not a question: ec5_* keys and the built-in title and timestamps."""
    return key.startswith("ec5_") or key in ("title", "created_at", "uploaded_at", "created_by")


# ---------------------------------------------------------------- maps (stored on the sync target)
def fmap(t):
    return json.loads(frappe.db.get_value(TARGET, t.name, "field_map") or "{}")


def tmap(t):
    return json.loads(frappe.db.get_value(TARGET, t.name, "typed_map") or "{}")


def location_key(t):
    return frappe.db.get_value(TARGET, t.name, "location_key") or ""


def fieldname(key, used):
    base = "q_" + re.sub(r"[^a-z0-9_]", "_", key.lower())
    if len(base) > 55:
        base = base[:48] + "_" + hashlib.md5(key.encode()).hexdigest()[:6]
    name, i = base, 2
    while name in used:
        name, i = f"{base[:50]}_{i}", i + 1
    return name


def _unique(name, used):
    out, i = name, 2
    while out in used:
        out, i = f"{name[:58]}_{i}", i + 1
    used.add(out)
    return out


def add_fields(t, keys, defn=None):
    """Create missing fields on the target's DocType for these API keys: one Small Text field per key
    (Small Text avoids MariaDB's row-size limit), plus typed companion columns for number, date and
    location questions. Returns (text fields created, keys that got new typed columns)."""
    defn, fm, tm = defn or {}, fmap(t), tmap(t)
    meta = frappe.get_meta(t.target_doctype, cached=False)
    used = set(fm.values()) | {df.fieldname for df in meta.fields}
    prev, fields, n_text, typed_keys = meta.fields[-1].fieldname, [], 0, []
    loc = location_key(t)
    for k in keys:
        if is_core(k):
            continue
        info = defn.get(k) or {}
        label = str(info.get("label") or k)
        if k not in fm:
            fm[k] = fn = fieldname(k, used)
            used.add(fn)
            fields.append({"fieldname": fn, "fieldtype": "Small Text", "label": label[:140],
                           "read_only": 1, "insert_after": prev})
            prev, n_text = fn, n_text + 1
        kind = KIND.get(info.get("type"))
        if kind and k not in tm:
            after, parts = fm[k], {}
            for part, suffix, ftype, lsuffix, extra in PARTS[kind]:
                tfn = _unique(fm[k][:64 - len(suffix)] + suffix, used)
                fields.append({"fieldname": tfn, "fieldtype": ftype, "label": f"{label[:120]} ({lsuffix})",
                               "read_only": 1, "insert_after": after, **extra})
                parts[part], after = tfn, tfn
            if prev == fm[k]:
                prev = after
            tm[k] = {"kind": kind, "fields": parts,
                     "dayfirst": str(info.get("datetime_format") or "").lower().startswith("dd")}
            typed_keys.append(k)
            if kind == "location" and not loc:
                loc = k  # the first location question also fills latitude/longitude, for the map view
    if fields:
        _create(t.target_doctype, fields)
        frappe.db.set_value(TARGET, t.name, {"field_map": json.dumps(fm), "typed_map": json.dumps(tm),
                                             "location_key": loc}, update_modified=False)
        frappe.clear_cache(doctype=t.target_doctype)
        ensure_nullable(t, tm)
        frappe.db.commit()
    return n_text, typed_keys


def ensure_nullable(t, tm=None):
    """Let number columns hold 'no answer'. Frappe creates Int and Float columns as NOT NULL DEFAULT 0, so an
    empty or unparseable answer would either fail to save or turn into 0, which reads as a real answer and
    skews counts and averages. Frappe leaves the column alone afterwards: it only alters a column when its
    type, default, index or uniqueness changes. Cheap to call: it only alters columns still NOT NULL."""
    tm = tmap(t) if tm is None else tm
    meta = frappe.get_meta(t.target_doctype)
    wanted = {fn for spec in tm.values() for fn in spec["fields"].values()} | {"latitude", "longitude"}
    wanted = {fn for fn in wanted if (df := meta.get_field(fn)) and df.fieldtype in ("Int", "Float")}
    if not wanted:
        return 0
    table = f"tab{t.target_doctype}"
    cols = frappe.db.sql("""select column_name, column_type from information_schema.columns
        where table_schema = database() and table_name = %s and is_nullable = 'NO'""", table)
    todo = [(c, ctype) for c, ctype in cols if c in wanted]
    if todo:
        frappe.db.sql_ddl(f"alter table `{table}` "
                          + ", ".join(f"modify `{c}` {ctype} null default null" for c, ctype in todo))
    return len(todo)


def _create(dt, fields):
    """Generated DocTypes get real fields in one save (one ALTER TABLE); the standard Epicollect Entry
    DocType gets Custom Fields, as app DocTypes can't be edited on a site."""
    if not frappe.db.get_value("DocType", dt, "custom"):
        create_custom_fields({dt: fields}, update=True)
        return
    doc = frappe.get_doc("DocType", dt)
    order = [df.fieldname for df in doc.fields]
    for f in fields:
        f = dict(f)
        after = f.pop("insert_after", None)
        doc.append("fields", f)
        order.insert(order.index(after) + 1 if after in order else len(order), f["fieldname"])
    pos = {fn: i for i, fn in enumerate(order)}
    doc.fields.sort(key=lambda df: pos[df.fieldname])
    for i, df in enumerate(doc.fields, 1):
        df.idx = i
    doc.save(ignore_permissions=True)


# ---------------------------------------------------------------- value conversion
def num(v, kind):
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


def to_date(v, dayfirst=False):
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


def location(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    if not isinstance(v, dict):
        return {}
    lat, lng = num(v.get("latitude"), "number"), num(v.get("longitude"), "number")
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return {}
    acc = num(v.get("accuracy"), "number")
    return {"lat": lat, "lng": lng, "acc": acc if acc is not None and acc >= 0 else None}


def typed(spec, v):
    """Typed column values for one answer: {fieldname: value}. Empty or unparseable answers give None."""
    kind, f = spec["kind"], spec["fields"]
    if kind == "location":
        loc = location(v)
        return {fn: loc.get(part) for part, fn in f.items()}
    if kind == "date":
        return {f["value"]: to_date(v, spec.get("dayfirst"))}
    return {f["value"]: num(v, kind)}


def typed_row(tm, loc_key, get):
    """All typed values for one entry; `get(key)` returns the raw answer."""
    vals = {}
    for k, spec in tm.items():
        vals.update(typed(spec, get(k)))
    if loc_key:
        loc = location(get(loc_key))
        vals.update(latitude=loc.get("lat"), longitude=loc.get("lng"))
    return vals


def backfill(t, keys):
    """Fill typed columns of existing entries from their text fields. Returns the number of entries changed."""
    fm, tm, loc_key = fmap(t), tmap(t), location_key(t)
    specs = {k: tm[k] for k in keys if k in tm and k in fm}
    if not specs:
        return 0
    use_loc = loc_key if loc_key in specs else ""
    cols = list(dict.fromkeys([fm[k] for k in specs]
                              + [fn for sp in specs.values() for fn in sp["fields"].values()]
                              + (["latitude", "longitude"] if use_loc else [])))
    last, changed = "", 0
    while True:
        rows = frappe.get_all(t.target_doctype, filters={"name": [">", last]}, fields=["name"] + cols,
                              order_by="name asc", limit_page_length=1000)
        if not rows:
            return changed
        for r in rows:
            vals = typed_row(specs, use_loc, lambda k, r=r: r.get(fm[k]))
            vals = {fn: v for fn, v in vals.items() if r.get(fn) != v}
            if vals:
                frappe.db.set_value(t.target_doctype, r["name"], vals, update_modified=False)
                changed += 1
        last = rows[-1]["name"]
        frappe.db.commit()
