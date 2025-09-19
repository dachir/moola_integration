import json, requests
import frappe
from datetime import datetime
from frappe.utils import getdate, nowdate, now_datetime, flt

APPROVED_DEFAULTS = {"1", "2"}  # from your payload examples

# ---------- helpers ----------
def _settings():
    s = frappe.get_single("Moola Settings")
    if not s.enabled:
        frappe.throw("Moola Integration is disabled in Moola Settings.")
    return s

def _auth_headers(s):
    headers = {"Accept": "application/json"}
    if s.auth_type == "Bearer" and s.api_key:
        headers["Authorization"] = f"Bearer {s.api_key}"
    elif s.auth_type == "ApiKey" and s.api_key:
        headers["x-api-key"] = s.api_key
    return headers

def _pick(obj, key, default=None):
    try:
        return obj.get(key, default)
    except Exception:
        return default

def _approved(s, exp):
    statuses = set([x.strip() for x in (s.approved_statuses or "").split(",") if x.strip()]) or APPROVED_DEFAULTS
    status = str(_pick(exp, "status"))
    if status not in statuses:
        return False
    if s.require_settled_cleared:
        if not (_pick(exp, "isSettled") and _pick(exp, "isCleared")):
            return False
    return True

def _posting_date(s, exp):
    if s.posting_date_policy == "Use expense.date":
        raw = _pick(exp, "date")
        try:
            if raw and "T" in str(raw):
                return getdate(str(raw).split("T")[0])
            return getdate(raw)
        except Exception:
            return getdate(nowdate())
    return getdate(nowdate())

def _category_map(s, exp):
    cat_field = s.category_key or "categoryID"
    cat_val = str(_pick(exp, cat_field) or "").strip()
    exp_acc = s.default_expense_account
    cc = s.default_cost_center
    br = s.default_branch
    for row in s.categories or []:
        if str(row.moola_category_key).strip().lower() == cat_val.lower():
            if row.expense_account:
                exp_acc = row.expense_account
            if row.cost_center:
                cc = row.cost_center
            if getattr(row, "branch", None):
                br = row.branch
            break
    return exp_acc, cc, br

def _card_account(s, exp):
    key_field = s.card_key or "ccMask"
    key_val = str(_pick(exp, key_field) or "").strip()
    for row in s.cards or []:
        if str(row.moola_card_key).strip() == key_val:
            return row.erpnext_card_account
    frappe.throw(f"No card account mapped for {key_field}='{key_val}'")

def _derive_branch(s, exp, fallback_branch=None):
    """
    Priority:
    1) Category map branch (if provided)
    2) Branch map via branch_key (costCenterID / costCenterName / userCode / nationalId)
    3) Default branch
    """
    if fallback_branch:
        # category map may pass it in
        cat_branch = fallback_branch
        if cat_branch:
            return cat_branch

    key_field = s.branch_key or "costCenterID"
    remote_key = str(_pick(exp, key_field) or "").strip().lower()
    if remote_key:
        for row in s.branches or []:
            if str(row.remote_branch_key or "").strip().lower() == remote_key:
                if row.branch:
                    return row.branch
    # default
    if s.default_branch:
        return s.default_branch
    frappe.throw(f"Branch is mandatory: no branch mapped for {key_field}='{remote_key}' and no default branch set.")

def _already_posted(exp_id):
    return bool(frappe.db.exists("Journal Entry", {"moola_transaction_id": str(exp_id)}))

def _amounts(s, exp):
    # choose net or total; optionally split VAT
    use = (s.use_amount_field or "total").lower()
    total = flt(_pick(exp, "total") or 0)
    net   = flt(_pick(exp, "net") or total)
    vat   = flt(_pick(exp, "vat") or 0)

    if use == "net":
        debit_expense = net
        extra_vat = vat if s.vat_account and vat > 0 else 0
        credit_total = net + extra_vat
    else:
        if s.vat_account and vat > 0:
            debit_expense = net
            extra_vat = vat
            credit_total = total
        else:
            debit_expense = total
            extra_vat = 0
            credit_total = total

    return debit_expense, extra_vat, credit_total

def _make_je(s, exp):
    exp_id = _pick(exp, "id")
    if not exp_id:
        return None, "no id"
    if _already_posted(exp_id):
        return None, "duplicate"
    if not _approved(s, exp):
        return None, "not approved"

    expense_acc, cost_center, cat_branch = _category_map(s, exp)
    branch = _derive_branch(s, exp, fallback_branch=cat_branch)
    card_acc = _card_account(s, exp)
    posting = _posting_date(s, exp)
    desc = _pick(exp, "note") or f"Moola expense {exp_id} – {_pick(exp, 'merchant') or ''}".strip()

    debit_expense, extra_vat, credit_total = _amounts(s, exp)
    if credit_total <= 0:
        return None, "zero amount"

    accounts = [
        {
            "account": expense_acc,
            "debit_in_account_currency": debit_expense,
            "credit_in_account_currency": 0,
            "cost_center": cost_center,
            "branch": branch
        }
    ]
    if extra_vat > 0:
        accounts.append({
            "account": s.vat_account,
            "debit_in_account_currency": extra_vat,
            "credit_in_account_currency": 0,
            "cost_center": cost_center,
            "branch": branch
        })
    accounts.append({
        "account": card_acc,
        "credit_in_account_currency": credit_total,
        "debit_in_account_currency": 0,
        "branch": branch
    })

    je = frappe.get_doc({
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "posting_date": posting,
        "company": s.company,
        "branch": branch,  # parent branch
        "user_remark": desc,
        "moola_transaction_id": str(exp_id),
        "accounts": accounts
    })
    je.insert(ignore_permissions=True)
    je.submit()
    return je.name, None

def _fetch_page(s, page_number, page_size, from_date=None):
    url = f"{s.api_base_url.rstrip('/')}/{s.expense_list_endpoint.lstrip('/')}"
    headers = _auth_headers(s)
    auth = None
    if s.auth_type == "Basic" and s.basic_username and s.basic_password:
        auth = (s.basic_username, s.basic_password)

    params = {
        "pageNumber": page_number,
        "pageSize": int(page_size or 100)
    }
    if from_date:
        # Adjust param name if Swagger specifies a different one
        params["dateFrom"] = str(from_date)

    r = requests.get(url, headers=headers, params=params, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_and_post_expenses(manual=False):
    s = _settings()

    log = frappe.get_doc({
        "doctype": "Moola Sync Log",
        "run_started_at": now_datetime(),
        "status": "Success",
        "fetched_count": 0,
        "created_je_count": 0,
        "skipped_count": 0,
        "message": ""
    }).insert(ignore_permissions=True)

    fetched = created = skipped = 0
    errors = []

    page = 1
    page_size = int(s.page_size or 100)

    from_date = None
    if s.last_success_time:
        from_date = getdate(s.last_success_time).isoformat()
    elif s.from_date:
        from_date = getdate(s.from_date).isoformat()

    while True:
        data = _fetch_page(s, page, page_size, from_date)
        items = (data or {}).get("data") or []
        fetched += len(items)

        for exp in items:
            try:
                if not _approved(s, exp):
                    skipped += 1
                    continue
                je_name, reason = _make_je(s, exp)
                if je_name:
                    created += 1
                else:
                    skipped += 1
            except Exception as e:
                skipped += 1
                errors.append(f"{exp.get('id')}: {e}")
                frappe.log_error(frappe.get_traceback(), "Moola JE create failed")

        has_next = (data or {}).get("hasNextPage")
        if not has_next:
            break
        page += 1
        if page > 10000:
            errors.append("Safety stop: too many pages")
            break

    s.last_success_time = now_datetime()
    s.save(ignore_permissions=True)

    log.fetched_count = fetched
    log.created_je_count = created
    log.skipped_count = skipped
    if errors:
        log.status = "Partial"
        log.message = "\n".join(errors)[:1400]
    log.save(ignore_permissions=True)

    return {"fetched": fetched, "created": created, "skipped": skipped, "errors": len(errors)}
