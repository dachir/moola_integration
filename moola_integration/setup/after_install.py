import re

import frappe


ACTIVE_ID_COLUMN = "_moola_active_transaction_id"
ACTIVE_ID_EXPRESSION = (
    "CASE WHEN docstatus != 2 THEN NULLIF(moola_transaction_id, '') ELSE NULL END"
)


def _ensure_active_unique_index():
    # Keep this generated column out of DocType metadata: Frappe must not write
    # it or turn it into an ordinary column during schema synchronization.
    if not frappe.db.has_column("Journal Entry", ACTIVE_ID_COLUMN):
        column_type = frappe.db.get_column_type("Journal Entry", "moola_transaction_id")
        if not re.fullmatch(r"varchar\(\d+\)", column_type, flags=re.IGNORECASE):
            frappe.throw("Moola Transaction ID must be a Data (varchar) field.")
        frappe.db.sql(f"""ALTER TABLE `tabJournal Entry`
            ADD COLUMN `{ACTIVE_ID_COLUMN}` {column_type}
            GENERATED ALWAYS AS ({ACTIVE_ID_EXPRESSION}) VIRTUAL""")
    # Database-calculated keys become NULL on cancellation, keeping the original
    # ID for audit history while allowing one replacement. This also protects
    # concurrent imports and changes made outside the sync engine.
    frappe.db.add_unique(
        "Journal Entry", [ACTIVE_ID_COLUMN], constraint_name="moola_active_transaction_unique"
    )


def _ensure_custom_field():
    filters = {"dt": "Journal Entry", "fieldname": "moola_transaction_id"}
    name = frappe.db.get_value("Custom Field", filters, "name")
    if name:
        duplicates = frappe.db.sql("""
            SELECT moola_transaction_id, COUNT(*) AS entry_count
            FROM `tabJournal Entry`
            WHERE docstatus != 2
                AND moola_transaction_id IS NOT NULL AND moola_transaction_id != ''
            GROUP BY moola_transaction_id HAVING COUNT(*) > 1
            LIMIT 20
        """, as_dict=True)
        if duplicates:
            ids = ", ".join(str(row.moola_transaction_id) for row in duplicates)
            frappe.throw(
                "Cannot enable Moola duplicate protection: multiple noncancelled "
                "Journal Entries already use these transaction IDs: " + ids +
                ". Reconcile the active entries before migrating again. "
                "Cancelled entries may keep their transaction IDs."
            )
        cf = frappe.get_doc("Custom Field", name)
    else:
        cf = frappe.get_doc({
            "doctype": "Custom Field",
            **filters,
            "label": "Moola Transaction ID",
            "fieldtype": "Data",
            "insert_after": "user_remark",
            "unique": 0,
            "read_only": 1,
            "no_copy": 1,
        })
        cf.insert(ignore_permissions=True)

    # Install active-only protection before removing the old global constraint.
    _ensure_active_unique_index()
    cf.unique = 0
    cf.read_only = 1
    cf.no_copy = 1
    cf.save(ignore_permissions=True)
    # Remove any remaining single-column unique indexes, including the index
    # added by earlier versions if metadata and the actual schema had diverged.
    while index := frappe.db.get_column_index(
        "tabJournal Entry", "moola_transaction_id", unique=True
    ):
        index_name = index.Key_name.replace("`", "``")
        frappe.db.sql(f"ALTER TABLE `tabJournal Entry` DROP INDEX `{index_name}`")
    frappe.clear_cache(doctype="Journal Entry")


def run():
    _ensure_custom_field()
