import frappe

def _ensure_custom_field():
    if frappe.db.exists("Custom Field", {"dt":"Journal Entry", "fieldname":"moola_transaction_id"}):
        return
    cf = frappe.get_doc({
        "doctype": "Custom Field",
        "dt": "Journal Entry",
        "fieldname": "moola_transaction_id",
        "label": "Moola Transaction ID",
        "fieldtype": "Data",
        "insert_after": "user_remark",
        "unique": 1,
        "read_only": 1,
        "no_copy": 1
    })
    cf.insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Journal Entry")

def run():
    _ensure_custom_field()
