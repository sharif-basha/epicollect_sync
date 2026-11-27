# Epicollect Sync (Frappe app)

Pulls Epicollect5 entries into Frappe. Built for Frappe/ERPNext **v15**.

## What you get
| DocType | Purpose |
|---|---|
| **Epicollect Settings** (single) | Project slug, client ID/secret, optional form ref and mapping index, enable switch, checkpoint. Has **Sync Now** and **Reset Checkpoint** buttons. |
| **Epicollect Entry** | One document per submission, named by `ec5_uuid`. Core fields plus **one field per Epicollect question** (Small Text, labelled with the question text). |
| **Epicollect Sync Log** | One row per run: status, new / changed / unchanged counts, duration, checkpoint, error trace. |

## Install
```bash
cd ~/frappe-bench
bench get-app https://github.com/<you>/frappe_epicollect_sync   # or: bench get-app /path/to/frappe_epicollect_sync
bench --site yoursite.local install-app epicollect_sync
bench --site yoursite.local migrate
bench --site yoursite.local enable-scheduler
```
Background workers must be running (`bench start` in development, supervisor in production) because syncs run on the `long` queue.

## Use
1. Open **Epicollect Settings**, fill in the project slug, client ID and secret, tick **Enabled**, Save.
2. Click **Sync Now**, then open **Epicollect Sync Log** to follow the run. After that it runs hourly.
3. The first run fetches everything. Later runs fetch only entries uploaded since the checkpoint (with a 5-minute overlap), insert new entries in bulk, and update only entries whose data changed (compared by a hash of the entry).
4. Each entry also keeps the entry exactly as received in **Raw Entry (JSON)**.

## Question fields
Each question gets its own field on Epicollect Entry, so you can filter, group and report on it in list views and reports.
- Click **Create Fields** on Epicollect Settings (optional, recommended before the first sync). It scans Epicollect, reads the question text from the project definition for the labels, and creates the fields in the background. Expect a few minutes for ~260 fields.
- If you skip it, the first sync creates fields as it meets new keys, and any question added to the form later gets its field automatically.
- Field names are generated (`q_` + the API key, lower-case, with a short hash if too long). The key-to-field map is stored in the hidden **Field Map** setting.
- Fields are Small Text so that hundreds of them fit in MariaDB's row-size limit. They always hold the answer exactly as received.

## Typed columns
For reports, charts and maps, some question types also get small typed columns next to their text field. The question type is read from the project definition.

| Epicollect question type | Extra columns |
|---|---|
| Integer | `<field>_num` (Int) |
| Decimal | `<field>_num` (Float) |
| Date | `<field>_date` (Date) |
| Location | `<field>_lat`, `<field>_lng` (Float, 7 decimals), `<field>_acc` (accuracy in metres) |

- An answer that can't be parsed (text in a number question, a whole number outside the Int range, coordinates out of range, an invalid date) leaves the typed column empty. The text field still has it.
- Dates are taken as they appear in Epicollect, with no timezone shift. Dates that are not in ISO form are read using the question's day/month order.
- Questions inside a branch are not part of the form's entries, so they get no fields yet.
- **Upgrading from a version without typed columns:** click **Create Fields** once. It adds the typed columns for questions you already sync, and fills them for entries already in Frappe from their text fields. New questions get typed columns automatically during a sync.
- The key-to-column map is stored in the hidden **Typed Field Map** setting.
- The fields are created as Custom Fields on the site, not in the app files. On a new site, run Create Fields again.
- Short, clean API keys make better fields: set up a custom mapping with short ASCII names in Epicollect and enter its number as **Mapping Index**.

## Notes
- Timestamps are stored as UTC.
- Only one sync or field-creation job runs at a time. If you click a button while one is running, you are told so instead of the job being queued.
- Entries are fetched in `uploaded_at` order, using the newest entry seen as the start of the next request (not page numbers), so entries edited during a run are never skipped. If Epicollect ever returns entries out of order, the run stops without moving the checkpoint.
- Network errors, HTTP 5xx and 429 are retried a few times with backoff, then the run fails and the next run resumes from the checkpoint.
- A run that was killed (job timeout, worker restart) is marked Failed at the start of the next run. Sync logs older than 90 days are deleted.
- Not synced yet: branch entries, child forms, and entries deleted in Epicollect.
- Never commit credentials. The client secret is stored encrypted by Frappe's Password field.
- Untested against a live Epicollect project. Try it on a development site first.

## Tests
The sync logic has stand-alone tests that use an in-memory fake of Frappe and of the Epicollect API, so they run without a bench:
```bash
pip install pytest requests python-dateutil
python -m pytest tests
```
