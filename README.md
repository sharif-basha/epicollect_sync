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
3. The first run fetches everything. Later runs fetch only entries uploaded since the checkpoint (with a 5-minute overlap), insert new entries, and update only entries whose data changed.

## Question fields
Each question gets its own field on Epicollect Entry, so you can filter, group and report on it in list views and reports.
- Click **Create Fields** on Epicollect Settings (optional, recommended before the first sync). It scans Epicollect, reads the question text from the project definition for the labels, and creates the fields in the background. Expect a few minutes for ~260 fields.
- If you skip it, the first sync creates fields as it meets new keys, and any question added to the form later gets its field automatically.
- Field names are generated (`q_` + the API key, lower-case, with a short hash if too long). The key-to-field map is stored in the hidden **Field Map** setting.
- Fields are Small Text so that hundreds of them fit in MariaDB's row-size limit. Numbers, dates and locations are stored as text.
- The fields are created as Custom Fields on the site, not in the app files. On a new site, run Create Fields again.
- Short, clean API keys make better fields: set up a custom mapping with short ASCII names in Epicollect and enter its number as **Mapping Index**.

## Notes
- Timestamps are stored as UTC.
- Only one sync runs at a time (the job is de-duplicated).
- Never commit credentials. The client secret is stored encrypted by Frappe's Password field.
- Untested against a live Epicollect project. Try it on a development site first.
