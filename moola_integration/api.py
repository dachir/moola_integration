import frappe
from .utils import fetch_and_post_expenses

@frappe.whitelist()
def sync_now():
    frappe.only_for("System Manager")
    res = fetch_and_post_expenses(manual=True)
    return f"Fetched {res.get('fetched',0)}, Created JE {res.get('created',0)}, Skipped {res.get('skipped',0)}"
