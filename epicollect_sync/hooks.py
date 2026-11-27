app_name = "epicollect_sync"
app_title = "Epicollect Sync"
app_publisher = "Your Name"
app_description = "Sync Epicollect5 project entries into Frappe"
app_email = "you@example.com"
app_license = "MIT"

# Runs hourly; skipped unless "Enabled" is ticked in Epicollect Settings.
scheduler_events = {"hourly": ["epicollect_sync.sync.scheduled"]}

# Delete sync logs older than 90 days (Log Settings can change this).
default_log_clearing_doctypes = {"Epicollect Sync Log": 90}
