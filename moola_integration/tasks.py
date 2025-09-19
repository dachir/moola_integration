import frappe
from moola_integration.utils import fetch_and_post_expenses

def sync_transactions():
    try:
        fetch_and_post_expenses()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Moola Scheduled Sync Failed")
