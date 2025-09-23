# moola_integration/utils.py

import json
import base64
import requests
import frappe
from datetime import timedelta
from frappe.utils import getdate, nowdate, now_datetime, flt

APPROVED_DEFAULTS = {"1", "2"}  # fallback approved statuses


# ---------- Settings ----------
def _settings():
    s = frappe.get_single("Moola Settings")
    if not s.enabled:
        frappe.throw("Moola Integration is disabled in Moola Settings.")
    return s


# ---------- Small helpers ----------
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
    if getattr(s, "require_settled_cleared", 0):
        if not (_pick(exp, "isSettled") and _pick(exp, "isCleared")):
            return False
    return True


def _posting_date(s, exp):
    if getattr(s, "posting_date_policy", "") == "Use expense.date":
        raw = _pick(exp, "date")
        try:
            if raw and "T" in str(raw):
                return getdate(str(raw).split("T")[0])
            return getdate(raw)
        except Exception:
            return getdate(nowdate())
    return getdate(nowdate())


def _category_map(s, exp):
    cat_field = getattr(s, "category_key", None) or "categoryID"
    cat_val = str(_pick(exp, cat_field) or "").strip()
    exp_acc = s.default_expense_account
    cc = s.default_cost_center
    br = s.default_branch
    for row in (s.categories or []):
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
    key_field = getattr(s, "card_key", None) or "ccMask"
    key_val = str(_pick(exp, key_field) or "").strip()
    for row in (s.cards or []):
        if str(row.moola_card_key).strip() == key_val:
            return row.erpnext_card_account
    frappe.throw(f"No card account mapped for {key_field}='{key_val}'")


def _derive_branch(s, exp, fallback_branch=None):
    """
    Priority:
      1) Category map branch (if provided)
      2) Branch map via branch_key (costCenterID / costCenterName / userCode / nationalId / userName)
      3) Default branch
    """
    if fallback_branch:
        cat_branch = fallback_branch
        if cat_branch:
            return cat_branch

    key_field = getattr(s, "branch_key", None) or "costCenterID"
    remote_key = str(_pick(exp, key_field) or "").strip().lower()
    if remote_key:
        for row in (s.branches or []):
            if str(row.remote_branch_key or "").strip().lower() == remote_key and row.branch:
                return row.branch

    if s.default_branch:
        return s.default_branch

    frappe.throw(f"Branch is mandatory: no branch mapped for {key_field}='{remote_key}' and no default branch set.")


def _already_posted(exp_id):
    return bool(frappe.db.exists("Journal Entry", {"moola_transaction_id": str(exp_id)}))


def _amounts(s, exp):
    """Return (debit_expense, extra_vat, credit_total) based on use_amount_field and VAT account."""
    use = (getattr(s, "use_amount_field", None) or "total").lower()
    total = flt(_pick(exp, "total") or 0)
    net = flt(_pick(exp, "net") or total)
    vat = flt(_pick(exp, "vat") or 0)

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


# ---------- Tag → Accounting Dimension ----------
def _tag_values(expense: dict):
    """Yield normalized tags from payload."""
    tags = expense.get("tags") or expense.get("tagList") or []
    for t in tags:
        yield {
            "tagName": t.get("tagName") or t.get("name") or "",
            "tagValueId": t.get("tagValueId") or t.get("valueId") or "",
            "tagValueName": t.get("tagValueName") or t.get("valueName") or "",
        }


def _dimensions_from_tags(expense: dict, settings) -> dict[str, str]:
    """
    Build {dimension_fieldname: dimension_value} from Tag → Dimension Map.
    Multiple tags can set multiple dimensions. First winner per field wins.
    """
    dim_map: dict[str, str] = {}

    rows = getattr(settings, "tag_dimension_map", []) or []
    if not rows:
        return dim_map

    # Pre-index settings rows by tag_name
    rows_by_tag = {}
    for r in rows:
        rows_by_tag.setdefault((r.tag_name or "").strip().upper(), []).append(r)

    for tag in _tag_values(expense):
        tname = (tag["tagName"] or "").strip().upper()
        if not tname or tname not in rows_by_tag:
            continue

        for r in rows_by_tag[tname]:
            # choose which key to compare
            if (r.match_on or "tagValueId") == "tagValueName":
                remote_val = tag["tagValueName"]
                ok = (remote_val or "").strip().lower() == (r.remote_value or "").strip().lower()
            else:
                remote_val = tag["tagValueId"]
                ok = str(remote_val or "") == str(r.remote_value or "")

            if not ok:
                continue

            fieldname = (r.dimension_fieldname or "").strip()
            value = (r.dimension_value or "").strip()
            if fieldname and value and fieldname not in dim_map:
                dim_map[fieldname] = value

    return dim_map


# ---------- HTTP ----------
def _basic_auth_header(user: str, pwd: str) -> str:
    token = base64.b64encode(f"{(user or '').strip()}:{(pwd or '').strip()}".encode()).decode()
    return f"Basic {token}"


def _fetch_page(s, page_number, page_size, from_date=None):
    """
    Exact match of your working Postman/httpie request:
      - GET
      - Query params: pageNumber, pageSize, FromDate, ToDate (YYYY-MM-DD)
      - Header: Authorization: Basic ...
      - No extra headers
    """
    url = f"{s.api_base_url.rstrip('/')}/{s.expense_list_endpoint.lstrip('/')}"

    # Authorization (build from decrypted Password field)
    headers = {}
    if getattr(s, "auth_type", "") == "Basic" and s.basic_username:
        pwd = s.get_password("basic_password")
        headers["Authorization"] = _basic_auth_header(s.basic_username, pwd)
    elif getattr(s, "auth_type", "") == "Bearer" and s.api_key:
        headers["Authorization"] = f"Bearer {s.api_key.strip()}"
    elif getattr(s, "auth_type", "") == "ApiKey" and s.api_key:
        headers["x-api-key"] = s.api_key.strip()

    # Params: keep as exact casings with date-only
    pn = int(page_number or 1)
    ps = int(page_size or 100)
    params = {"pageNumber": pn, "pageSize": ps}

    if from_date:
        fd = getdate(from_date).isoformat()  # YYYY-MM-DD
        td = getdate(nowdate()).isoformat()
        params.update({"FromDate": fd, "ToDate": td})

    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Moola Sync: network error")
        raise

    if r.status_code >= 400:
        # Log both intended and actual request headers for perfect visibility
        try:
            actual_req_headers = dict(getattr(r.request, "headers", {}))
        except Exception:
            actual_req_headers = {}
        detail = {
            "url": r.url,
            "status_code": r.status_code,
            "response_text": (r.text or "")[:5000],
            "response_headers": dict(r.headers),
            "sent_headers": headers,
            "actual_request_headers": actual_req_headers,
            "sent_params": params,
        }
        frappe.log_error(json.dumps(detail, ensure_ascii=False, indent=2), "Moola Sync: HTTP error")
        raise frappe.ValidationError(
            f"Remote API error {r.status_code}. See Error Log: 'Moola Sync: HTTP error'."
        )

    # JSON parse (server might send JSON with any content-type)
    try:
        return r.json()
    except Exception:
        try:
            return json.loads(r.text)
        except Exception:
            frappe.log_error({"url": r.url, "body": (r.text or "")[:5000]}, "Moola Sync: invalid JSON")
            raise frappe.ValidationError("Remote API returned non-JSON response. Check Error Log.")
@frappe.whitelist()
def fetch_and_post_expenses_from(from_date, advance_cursor=False):
    """
    One-off sync starting at `from_date` (date or YYYY-MM-DD string).
    - No look-back window is applied.
    - Does not advance last_success_time unless advance_cursor=True.
    """
    s = _settings()

    # local logger entry
    log = frappe.get_doc({
        "doctype": "Moola Sync Log",
        "run_started_at": now_datetime(),
        "status": "Success",
        "fetched_count": 0,
        "created_je_count": 0,
        "skipped_count": 0,
        "message": f"Manual run from {getdate(from_date).isoformat()}",
    }).insert(ignore_permissions=True)

    fetched = created = skipped = 0
    errors = []

    page = 1
    page_size = int(getattr(s, "page_size", None) or 100)
    from_date = getdate(from_date)  # ensure date obj

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
            except Exception:
                skipped += 1
                errors.append(f"{_pick(exp,'id')}: see 'Moola JE create failed'")
                frappe.log_error(frappe.get_traceback(), "Moola JE create failed")

        if not ((data or {}).get("hasNextPage")):
            break

        page += 1
        if page > 10000:
            errors.append("Safety stop: too many pages")
            break

    # cursor policy for this manual run
    if advance_cursor and len(errors) == 0 and (created > 0 or fetched == 0):
        s.last_success_time = now_datetime()
        s.save(ignore_permissions=True)

    # finalize log
    log.fetched_count = fetched
    log.created_je_count = created
    log.skipped_count = skipped
    if errors:
        log.status = "Partial"
        log.message = "\n".join(errors)[:1400]
    log.save(ignore_permissions=True)

    return {"fetched": fetched, "created": created, "skipped": skipped, "errors": len(errors)}


# ---------- JE creation ----------
def _make_je(s, exp):
    exp_id = _pick(exp, "id")
    if not exp_id:
        return None, "no id"
    if _already_posted(exp_id):
        return None, "duplicate"
    if not _approved(s, exp):
        return None, "not approved"

    expense_acc, cost_center, cat_branch = _category_map(s, exp)
    dim_map = _dimensions_from_tags(exp, s)
    branch = _derive_branch(s, exp, fallback_branch=cat_branch)
    card_acc = _card_account(s, exp)
    posting = _posting_date(s, exp)
    desc = (_pick(exp, "note") or f"Moola expense {exp_id} – {_pick(exp, 'merchant') or ''}").strip()

    debit_expense, extra_vat, credit_total = _amounts(s, exp)
    if credit_total <= 0:
        return None, "zero amount"

    accounts = [
        {
            "account": expense_acc,
            "debit_in_account_currency": debit_expense,
            "credit_in_account_currency": 0,
            "cost_center": cost_center,
            "branch": branch,
        }
    ]
    if extra_vat > 0:
        accounts.append(
            {
                "account": s.vat_account,
                "debit_in_account_currency": extra_vat,
                "credit_in_account_currency": 0,
                "cost_center": cost_center,
                "branch": branch,
            }
        )
    accounts.append(
        {
            "account": card_acc,
            "credit_in_account_currency": credit_total,
            "debit_in_account_currency": 0,
            "branch": branch,
        }
    )

    # Apply dimensions to each JE line
    if dim_map:
        for line in accounts:
            line.update(dim_map)

    je = frappe.get_doc(
        {
            "doctype": "Journal Entry",
            "voucher_type": "Journal Entry",
            "posting_date": posting,
            "company": s.company,
            "branch": branch,  # parent branch
            "user_remark": desc,
            "moola_transaction_id": str(exp_id),
            "accounts": accounts,
        }
    )
    je.insert(ignore_permissions=True)
    je.submit()
    return je.name, None


# ---------- Main sync ----------
def fetch_and_post_expenses(manual=False):
    s = _settings()
    lookback_days = int(getattr(s, "resync_lookback_days", 7) or 0)

    log = frappe.get_doc(
        {
            "doctype": "Moola Sync Log",
            "run_started_at": now_datetime(),
            "status": "Success",
            "fetched_count": 0,
            "created_je_count": 0,
            "skipped_count": 0,
            "message": "",
        }
    ).insert(ignore_permissions=True)

    fetched = created = skipped = 0
    errors: list[str] = []

    page = 1
    page_size = int(getattr(s, "page_size", None) or 100)

    # Compute from_date as a date object (not string) to avoid type issues
    from_date = None
    if getattr(s, "last_success_time", None):
        from_date = getdate(s.last_success_time)
    elif getattr(s, "from_date", None):
        from_date = getdate(s.from_date)

    # Rolling look-back to reprocess previously skipped items
    if lookback_days > 0:
        lb = getdate(nowdate()) - timedelta(days=lookback_days)
        from_date = (from_date and max(lb, from_date)) or lb

    # Sync loop
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
            except Exception:
                skipped += 1
                # Log full traceback; also store a concise error for the run summary
                tb = frappe.get_traceback()
                exp_id = _pick(exp, "id")
                errors.append(f"{exp_id}: see 'Moola JE create failed'")
                frappe.log_error(tb, "Moola JE create failed")

        has_next = (data or {}).get("hasNextPage")
        if not has_next:
            break
        page += 1
        if page > 10000:
            errors.append("Safety stop: too many pages")
            break

    # Advance cursor only on clean runs (or if nothing fetched)
    advance_cursor = (len(errors) == 0) and (created > 0 or fetched == 0)
    if advance_cursor:
        s.last_success_time = now_datetime()
        s.save(ignore_permissions=True)

    # Finalize log
    log.fetched_count = fetched
    log.created_je_count = created
    log.skipped_count = skipped
    if errors:
        log.status = "Partial"
        log.message = "\n".join(errors)[:1400]
    log.save(ignore_permissions=True)

    return {"fetched": fetched, "created": created, "skipped": skipped, "errors": len(errors)}
