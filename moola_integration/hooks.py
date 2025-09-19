from . import __version__ as app_version

app_name = "moola_integration"
app_title = "Moola Integration"
app_publisher = "Paavan Infotech"
app_description = "Moola Integration for ERPNext"
app_email = "sales@paavaninfotech.com"
app_license = "MIT"

# Run every 15 minutes
scheduler_events = {
    "cron": {
        "*/15 * * * *": [
            "moola_integration.tasks.sync_transactions"
        ],
    }
}

# Quick button on Settings
doctype_js = {
    "Moola Settings": "public/js/moola_settings.js"
}

after_install = "moola_integration.setup.after_install.run"
