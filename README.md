# Epicollect Sync (Frappe app)

Pulls Epicollect5 entries into Frappe, for any number of projects. Every form and every branch question of a
project gets its own DocType, with one field per question. Built for Frappe/ERPNext **v15**.

## What you get
| DocType | Purpose |
|---|---|
| **Epicollect Project** | One per Epicollect project: slug, client ID/secret, how often to sync, deletion checks, email alerts. Buttons: **Sync Now**, **Check for Deleted Entries Now**, **Reset Checkpoints**, **Test Connection**, **Set Up Forms and Fields**, **Open Dashboard**. |
| *Generated DocTypes* | One per form and one per branch question, named `<prefix> <form name>` and `<form DocType> <branch question>`. One record per entry, named by its Epicollect UUID. |
| **Epicollect Sync Target** | Managed by the app: one per synced form or branch, holding its DocType, checkpoint and field maps. Users never edit it, so an open form can't overwrite the sync state. |
| **Epicollect Sync Log** | One row per run: status, new / changed / unchanged / deleted counts, a row per form, duration, error trace. |
| **Epicollect** workspace | Sync health (entries added, syncs and failures this week, daily charts) and shortcuts. |
| **Epicollect Viewer** role | Read, report and export access to the synced entries, without System Manager. |

Each generated DocType has:
- **Entry UUID**, **Project**, **Title**, **Created At** / **Uploaded At** (UTC), **Created By**.
- **Parent Entry** (child forms and branches): a link to the entry they belong to, so the parent shows its children under *Connections*.
- **Deleted in Epicollect**: set by the deletion check (entries are flagged, never removed).
- **Latitude** / **Longitude**: filled from the form's first location question, so the list has a **Map** view.
- **Raw Entry (JSON)**: the entry exactly as received.
- One **Small Text** field per question, holding the answer exactly as received, plus typed columns (below).

## Install
```bash
cd ~/frappe-bench
bench get-app https://github.com/<you>/epicollect_sync
bench --site yoursite.local install-app epicollect_sync
bench --site yoursite.local enable-scheduler
```
Background workers must be running (`bench start` in development, supervisor in production): syncs run on the
`long` queue. Email alerts need an outgoing Email Account.

## Use
1. Create an **Epicollect Project**: the slug (the part after `/project/` in the project's address), client ID and
   secret. Optionally set **DocType Name Prefix** (for example `Household Survey`) before the first sync.
2. Click **Set Up > Test Connection** to check the credentials, then **Sync > Sync Now**.
   The first sync creates the DocTypes, fields and dashboard, then fetches every entry. You can also run
   **Set Up Forms and Fields** first to create everything before any data arrives.
3. Follow the run in **Epicollect Sync Log**. After that the project syncs on its own every **Sync Every** minutes.

Later runs fetch only entries uploaded since each form's checkpoint (with a 5-minute overlap), insert new ones in
bulk and update only those whose data changed. Forms, branches and questions added to the project in Epicollect
are picked up automatically on the next sync.

## Typed columns
Some question types also get typed columns next to their text field, for filters, charts and maps. The type comes
from the project definition.

| Epicollect question type | Extra columns |
|---|---|
| Integer | `<field>_num` (Int) |
| Decimal | `<field>_num` (Float) |
| Date | `<field>_date` (Date) |
| Location | `<field>_lat`, `<field>_lng` (Float, 7 decimals), `<field>_acc` (accuracy in metres) |

- An answer that can't be parsed (text in a number question, a whole number outside the Int range, coordinates
  out of range, an invalid date) leaves the typed column empty. The text field still has it.
- Dates are used as they appear in Epicollect, with no timezone shift. Dates not in ISO form are read using the
  question's day/month order.
- Field names are generated (`q_` + the API key, lower-case, with a short hash if too long). Short, clean API keys
  make better names: set up a custom mapping with short ASCII names in Epicollect and enter its number as
  **Mapping Index**.

## Deleted entries
Epicollect's export has no list of deletions, so the app finds them by re-reading every entry: weekly by default
(**Check for Deleted Entries**: Weekly, Daily or Never), or on demand with **Check for Deleted Entries Now**.
Entries no longer returned are flagged **Deleted in Epicollect**; if one comes back, the flag is cleared. As a
safety net, nothing is flagged if Epicollect returns fewer than half of the entries already synced; the log says so.

## Alerts
With **Email Alerts To** filled in, the app emails:
- when a project's sync starts failing, and once more when it works again (not on every failed run);
- daily, when no new entries have arrived for **Alert if No New Entries for (days)**, although syncs work.

## Dashboards
- **Epicollect** workspace: entries added, syncs and failed syncs over the last 7 days, and daily charts.
- **Epicollect &lt;project&gt;** dashboard, one per project (**Open Dashboard**): for each form and branch, total
  entries, entries in the last 7 days and entries per day; for the main form, entries by collector. They are created
  once and then yours to edit; build more from the typed columns with **Dashboard Chart** or **Report Builder**.
- **Map** view on every generated DocType: `/app/<doctype>/view/map`.

## Upgrading from 0.1
`bench --site yoursite.local migrate` moves the old **Epicollect Settings** into an **Epicollect Project**
(credentials, checkpoint, field maps), keeps your data in the existing **Epicollect Entry** DocType for the first
form, and removes Epicollect Settings. The next sync adds DocTypes for any other forms and branches.

## Notes
- Only one sync or set-up runs at a time per project. If you click a button while one is running, you are told so.
- Entries are fetched in `uploaded_at` order, using the newest entry seen as the start of the next request (not
  page numbers), so entries edited during a run are never skipped. If Epicollect ever returns entries out of
  order, the run stops without moving the checkpoint.
- Network errors, HTTP 5xx and 429 are retried with backoff. A failure in one form is logged and the other forms
  still sync. A killed run (timeout, worker restart) is marked Failed at the start of the next one. Sync logs
  older than 90 days are deleted.
- Never commit credentials. The client secret is stored encrypted by Frappe's Password field.
- Not synced: photos, audio and video files (only the answers), and anything from Frappe back to Epicollect.
- Tested on Frappe 15 with a fake Epicollect API, not against a live project yet. Try it on a development site first.

## Tests
Integration tests run on a real site against a fake Epicollect API:
```bash
bench --site test.local set-config allow_tests true
bench --site test.local run-tests --app epicollect_sync
```
CI (`.github/workflows/ci.yml`) runs them with lint on every push.
