app_name = "epicollect_sync"
app_title = "Epicollect Sync"
app_publisher = "Epicollect Sync contributors"
app_description = "Sync Epicollect5 project entries into Frappe"
app_email = "noreply@example.com"
app_license = "MIT"
required_apps = ["frappe"]

before_install = "epicollect_sync.install.before_install"
after_migrate = "epicollect_sync.install.after_migrate"

# Every 5 minutes: queues each enabled project whose own interval (Sync Every) has passed.
scheduler_events = {
    "cron": {"*/5 * * * *": ["epicollect_sync.sync.scheduled"]},
    "daily": ["epicollect_sync.notify.check_inactivity"],
}

# Delete sync logs older than 90 days (Log Settings can change this).
default_log_clearing_doctypes = {"Epicollect Sync Log": 90}
