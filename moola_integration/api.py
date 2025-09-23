import frappe
from .utils import fetch_and_post_expenses
from frappe.utils import getdate
from . import utils
from . import utils

@frappe.whitelist()
def sync_now():
    frappe.only_for("System Manager")
    res = fetch_and_post_expenses(manual=True)
    return f"Fetched {res.get('fetched',0)}, Created JE {res.get('created',0)}, Skipped {res.get('skipped',0)}"

@frappe.whitelist()
def sync_from_date(from_date: str, advance_cursor: int = 0):
    """
    Run a one-off sync starting from `from_date` (YYYY-MM-DD).
    Does NOT change last_success_time unless `advance_cursor=1`.
    Returns the same stats dict as the normal sync.
    """
    dt = getdate(from_date)  # validates & normalizes
    res = utils.fetch_and_post_expenses_from(dt, advance_cursor=bool(int(advance_cursor or 0)))
    return res
