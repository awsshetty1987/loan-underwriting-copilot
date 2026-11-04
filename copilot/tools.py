"""The plain-Python tools (NB01 + NB04). The agents reach them only through the MCP server (mcp_server.py).
Everything here is deterministic code except extract_income_fields, which asks GPT and checks its answer."""
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime

import httpx

from . import storage
from .config import settings
from .llm import call_llm, parse_json

APPLICANT_ID_PATTERN = re.compile(r"APP_\d{4}")
PAN_PATTERN = re.compile(r"[A-Z]{5}[0-9]{4}[A-Z]")
# The documents a customer gives us, by type. A PDF is used if there is one; the text versions are the older
# notebook format (NB01 to NB04) and still work. Scanned photos need OCR (Textract): not yet.
DOCUMENTS = {"income": ["salary_slip.pdf", "salary_certificate.txt"],
             "bank-statements": ["statement_6m.pdf", "statement_6m.csv"],
             "kyc": ["id_proof.pdf", "id_proof.txt"]}
SALARY_KEYWORDS = ["SALARY", "SAL ", "SAL/", "SAL-", "PAYROLL"]


class IdentityMismatchError(ValueError):
    """KYC and salary slip don't belong to the same person: stop before calling the bureau."""


# ---------- small helpers ----------

def inr(amount) -> str:
    """Rupees with Indian digit grouping: 1500000 -> Rs.15,00,000."""
    digits = f"{abs(float(amount)):.0f}"
    head, tail = digits[:-3], digits[-3:]
    while len(head) > 2:
        head, tail = head[:-2], head[-2:] + "," + tail
    return ("-" if float(amount) < 0 else "") + "Rs." + (f"{head},{tail}" if head else tail)


def to_number(value):
    """'Rs.1,20,000' -> 120000.0, 120000 -> 120000.0, None/'' -> None"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = re.sub(r"[^0-9.]", "", str(value).replace("Rs.", ""))
    return float(digits) if digits else None


def check_applicant_id(applicant_id: str) -> str:
    if not APPLICANT_ID_PATTERN.fullmatch(applicant_id or ""):
        raise ValueError(f"Invalid applicant_id {applicant_id!r}: expected the form APP_1234")
    return applicant_id


def list_applicants() -> list[str]:
    """Applicant folders that hold at least one document (audit-only folders don't count)."""
    ids = {k.split("/")[1] for k in storage.list_keys("loan-docs/") if "/audit/" not in k and k.count("/") >= 2}
    return sorted(i for i in ids if APPLICANT_ID_PATTERN.fullmatch(i))


# ---------- documents ----------

def find_document(applicant_id: str, doc_type: str) -> tuple[str, bytes]:
    """(file name, content) of the applicant's document of this type. Raises FileNotFoundError if none."""
    check_applicant_id(applicant_id)
    if doc_type not in DOCUMENTS:
        raise ValueError(f"doc_type must be one of {sorted(DOCUMENTS)}")
    for filename in DOCUMENTS[doc_type]:
        try:
            return filename, storage.get_bytes(f"loan-docs/{applicant_id}/{doc_type}/{filename}")
        except FileNotFoundError:
            continue
    raise FileNotFoundError(f"Missing document for {applicant_id}: {doc_type}/{DOCUMENTS[doc_type][0]}")


def _pdf(content: bytes):
    import pdfplumber
    try:
        return pdfplumber.open(io.BytesIO(content))
    except Exception as e:
        raise ValueError(f"Could not read the PDF: {e}") from None


def pdf_to_text(content: bytes) -> str:
    """The PDF as 'Label: value' lines, the same shape as the old text documents. Each table becomes a section:
    its header's first cell is the section name, each row 'label: amount'."""
    with _pdf(content) as pdf:
        lines, sections = [], []
        for page in pdf.pages:
            lines += [ln.strip() for ln in (page.extract_text() or "").splitlines() if ":" in ln]
            for table in page.extract_tables():
                if table and table[0] and table[0][0]:
                    sections.append(table[0][0].strip().upper())
                    sections += [f"{row[0].strip()}: {row[-1].strip()}" for row in table[1:] if row and row[0]]
    return "\n".join(lines + sections)


def get_document(applicant_id: str, doc_type: str) -> str:
    """The document as text (a PDF is converted). doc_type: 'income' | 'bank-statements' | 'kyc'"""
    filename, content = find_document(applicant_id, doc_type)
    return pdf_to_text(content) if filename.endswith(".pdf") else content.decode("utf-8")


def find_missing_documents(applicant_id: str) -> list[str]:
    """The required documents that are not on file (empty list = all present)."""
    missing = []
    for doc_type, names in DOCUMENTS.items():
        try:
            find_document(applicant_id, doc_type)
        except FileNotFoundError:
            missing.append(f"{doc_type}/{names[0]}")
    return missing


def parse_salary_certificate(raw_text: str) -> dict:
    """Plain-text reader for the payslip: names, PAN, dates, every earnings/deduction line, gross, net."""
    fields = {"declared_income": None, "net_pay": None, "total_deductions": None, "employer": None, "name": None,
              "pan": None, "date_of_joining": None, "pay_date": None, "earnings": {}, "deductions": {}}
    section = None
    simple = {"Employee Name": "name", "Employer": "employer", "PAN": "pan", "Date of Joining": "date_of_joining",
              "Pay Date": "pay_date", "Date Issued": "pay_date"}
    for line in raw_text.splitlines():
        line = line.strip()
        if line.upper() in ("EARNINGS", "DEDUCTIONS"):
            section = line.lower()
            continue
        if ":" not in line:
            continue
        label, value = [p.strip() for p in line.split(":", 1)]
        if label in simple:
            fields[simple[label]] = value.upper() if label == "PAN" else value
        elif label in ("Gross Pay", "Declared Monthly Income"):
            fields["declared_income"] = to_number(value)
        elif label == "Total Deductions":
            fields["total_deductions"] = to_number(value)
        elif label == "Net Pay":
            fields["net_pay"] = to_number(value)
        elif section in ("earnings", "deductions"):
            fields[section][label] = to_number(value)
    return fields


SLIP_PROMPT = """Extract these fields from the salary slip below. Return a JSON object with exactly these keys:
  "declared_income": gross monthly pay as a plain number
  "net_pay": net pay as a plain number
  "total_deductions": total deductions as a plain number
  "earnings": object of each earnings line, label -> number (do not include Gross Pay)
  "deductions": object of each deduction line, label -> number (do not include Total Deductions)
  "employer": employer name as plain text, "name": employee name as plain text,
  "pan": PAN as plain text like ABCDE1234F, or null
  "date_of_joining": YYYY-MM-DD or null, "pay_date": YYYY-MM-DD or null
Copy the numbers exactly as printed, even if they do not add up.
Document:
---
{document}
---"""


def extract_income_fields(raw_text: str) -> dict:
    """GPT reads the payslip; each value is used only if it has the right shape, else the plain-text reader's.
    (Phase 2: the same fields come from Textract or GPT vision on a PDF or photo.)"""
    plain = parse_salary_certificate(raw_text)
    try:
        answer = call_llm("You extract structured data from documents. Reply with JSON only.",
                          SLIP_PROMPT.format(document=raw_text), json_mode=True, purpose="read payslip",
                          pii_names=[plain.get("name")])
    except Exception:                                    # OpenAI down or over quota: plain-text reader
        answer = None
    if answer:
        try:
            data = parse_json(answer)
            fields = dict(plain)
            for key in ["declared_income", "net_pay", "total_deductions"]:
                if isinstance(data.get(key), (int, float, str)) and to_number(data[key]) is not None:
                    fields[key] = to_number(data[key])
            for key in ["earnings", "deductions"]:
                if isinstance(data.get(key), dict) and data[key]:
                    fields[key] = {k: to_number(v) for k, v in data[key].items()
                                   if isinstance(v, (int, float, str)) and not re.search(r"gross|total|net", k, re.I)}
            for key in ["employer", "name", "date_of_joining", "pay_date", "pan"]:
                value = data.get(key)
                fields[key] = value.strip() if isinstance(value, str) and value.strip() else plain[key]
            fields["pan"] = (fields["pan"] or "").upper() or None
            if fields["declared_income"] is not None:
                return {**fields, "extracted_by": "llm"}
        except (ValueError, AttributeError, TypeError):
            pass
    return {**plain, "extracted_by": "fallback"}


# ---------- identity and credit bureau ----------

def get_kyc_details(applicant_id: str) -> dict:
    text = get_document(applicant_id, "kyc")
    labels = {"Name": "name", "PAN": "pan", "Aadhaar (masked)": "aadhaar_masked", "Address": "address"}
    kyc = {}
    for line in text.splitlines():
        if ":" in line:
            label, value = line.split(":", 1)
            if label.strip() in labels:
                kyc[labels[label.strip()]] = value.strip()
    kyc["pan"] = kyc.get("pan", "").upper()
    if not PAN_PATTERN.fullmatch(kyc["pan"]):
        raise ValueError(f"No valid PAN in KYC for {applicant_id}")
    return kyc


def verify_identity(applicant_id: str, slip: dict | None = None) -> dict:
    """KYC PAN must equal the salary-slip PAN. slip = the payslip fields if already read (saves a GPT call)."""
    kyc = get_kyc_details(applicant_id)
    if slip is None:
        slip = extract_income_fields(get_document(applicant_id, "income"))
    slip_pan = (slip.get("pan") or "").upper()
    if slip_pan != kyc["pan"]:
        raise IdentityMismatchError(f"{applicant_id}: PAN on salary slip ({slip_pan or 'missing'}) "
                                    f"does not match KYC PAN ({kyc['pan']})")
    same_name = " ".join((slip.get("name") or "").lower().split()) == " ".join(kyc.get("name", "").lower().split())
    return {"pan": kyc["pan"], "name": kyc.get("name"), "pan_match": True, "name_match": same_name}


def get_credit_bureau_report(applicant_id: str, slip: dict | None = None) -> dict:
    identity = verify_identity(applicant_id, slip)
    cfg = settings()
    resp = httpx.post(cfg.bureau_url, json={"pan": identity["pan"], "name": identity["name"]},
                      headers={"x-api-key": cfg.bureau_api_key}, timeout=30.0)
    if resp.status_code == 404:
        raise LookupError(f"No credit report for {applicant_id} (PAN {identity['pan']})")
    resp.raise_for_status()
    return resp.json()


NEW_TO_BANK = {"existing_customer": False, "tenure_months": 0, "past_loans_with_bank": 0, "past_loan_defaults": 0}


def get_core_banking_history(applicant_id: str) -> dict:
    """Asks the bank's core-banking system, by the PAN on the KYC. Not found = new to bank, not an error."""
    check_applicant_id(applicant_id)
    pan = get_kyc_details(applicant_id)["pan"]
    cfg = settings()
    resp = httpx.post(cfg.core_banking_url, json={"pan": pan},
                      headers={"x-api-key": cfg.core_banking_api_key}, timeout=30.0)
    if resp.status_code == 404:
        return {"applicant_id": applicant_id, "pan": pan, **NEW_TO_BANK}
    resp.raise_for_status()
    return resp.json()


def get_policy_rules() -> dict:
    """The rules, plus 'rules_version': a fingerprint of the file, saved with every decision so an auditor can
    tell exactly which limits were in force (a 'version' label in the file, if any, is shown in front)."""
    text = storage.get_text("loan-policy/rules.json")
    rules = json.loads(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    rules["rules_version"] = f"{rules['version']}@{digest}" if rules.get("version") else f"sha256:{digest}"
    return rules


def get_loan_request(applicant_id: str) -> dict | None:
    """What the customer asked for: loan_amount, tenure_months, interest_rate, purpose, application_date."""
    check_applicant_id(applicant_id)
    try:
        return json.loads(storage.get_text(f"loan-docs/{applicant_id}/application/loan_request.json"))
    except FileNotFoundError:
        return None


# ---------- bank statement and income ----------

def is_salary_credit(description: str, employer: str | None = None) -> bool:
    desc = " " + description.upper().strip() + " "
    if any(k in desc for k in SALARY_KEYWORDS):
        return True
    return bool(employer) and employer.upper() in desc


def get_statement_rows(applicant_id: str) -> list[dict]:
    """Statement rows with date, description, debit, credit, balance (None if the file has no balance column).
    Reads the PDF statement's table, or the older CSV formats (type + amount, or debit + credit + balance)."""
    filename, content = find_document(applicant_id, "bank-statements")
    if filename.endswith(".pdf"):
        with _pdf(content) as pdf:
            table = [row for page in pdf.pages for t in page.extract_tables() for row in t]
        header = [c.strip().lower() for c in table[0]] if table else []
        records = [dict(zip(header, row, strict=False)) for row in table if row and row[0] and row[0][:1].isdigit()]
    else:
        records = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    rows = []
    for r in records:
        if "amount" in r:
            amount = to_number(r["amount"]) or 0.0
            debit, credit = (amount, 0.0) if r["type"].strip().lower() == "debit" else (0.0, amount)
        else:
            debit, credit = to_number(r.get("debit")) or 0.0, to_number(r.get("credit")) or 0.0
        balance = to_number(r.get("balance"))
        rows.append({"date": r["date"].strip(), "description": r["description"].strip(), "debit": debit,
                     "credit": credit, "balance": balance})
    return rows


def get_salary_credits(applicant_id: str, employer: str | None = None) -> dict:
    """Median monthly salary credited, and in how many of the statement's months salary arrived."""
    rows = [r for r in get_statement_rows(applicant_id) if r["description"] != "OPENING BALANCE"]
    months_in_statement = {r["date"][:7] for r in rows}
    salary_by_month: dict[str, float] = {}
    for r in rows:
        if r["credit"] > 0 and is_salary_credit(r["description"], employer):
            salary_by_month[r["date"][:7]] = salary_by_month.get(r["date"][:7], 0) + r["credit"]
    amounts = sorted(salary_by_month.values())
    return {"monthly_salary": amounts[len(amounts) // 2] if amounts else 0.0,
            "salary_months": len(salary_by_month), "statement_months": len(months_in_statement),
            "salary_by_month": dict(sorted(salary_by_month.items()))}


def check_consistency(declared_income, salary: dict, rules: dict, net_pay=None) -> list[str]:
    """Salary credited vs NET pay on the slip (what really reaches the bank). Falls back to gross if no net pay."""
    if to_number(net_pay):
        expected, label = to_number(net_pay), "net pay on slip"
    else:
        expected, label = to_number(declared_income), "declared"
    if not expected:
        return ["Declared income missing from salary certificate"]
    if salary["salary_months"] == 0:
        return ["No salary credit found in bank statement"]
    flags = []
    ratio = salary["monthly_salary"] / expected
    if ratio < rules["min_income_consistency_ratio"]:
        major = ratio < rules.get("major_income_mismatch_ratio", 0.6)
        flags.append(f"{'MAJOR income' if major else 'Income'} mismatch: {label} {inr(expected)}/mo, "
                     f"salary credited {inr(salary['monthly_salary'])}/mo ({ratio * 100:.0f}% of {label})")
    if salary["salary_months"] < salary["statement_months"]:
        flags.append(f"Salary credited in only {salary['salary_months']} of {salary['statement_months']} months")
    return flags


# ---------- document checks (consistency, not proof of authenticity) ----------

def check_slip_arithmetic(slip: dict) -> list[str]:
    """Earnings add up to gross, deductions add up to the total, gross - deductions = net."""
    gross, total, net = slip.get("declared_income"), slip.get("total_deductions"), slip.get("net_pay")
    if not slip.get("earnings") or net is None or total is None or gross is None:
        return ["Salary slip has no earnings/deductions breakdown: arithmetic not checked"]
    problems = []
    if abs(sum(slip["earnings"].values()) - gross) > 1:
        problems.append(f"earnings add up to {inr(sum(slip['earnings'].values()))}, not the gross {inr(gross)}")
    if abs(sum(slip["deductions"].values()) - total) > 1:
        problems.append(f"deductions add up to {inr(sum(slip['deductions'].values()))}, not {inr(total)}")
    if abs(gross - total - net) > 1:
        problems.append(f"gross {inr(gross)} - deductions {inr(total)} = {inr(gross - total)}, "
                        f"but net pay shows {inr(net)}")
    return [f"MAJOR document: salary slip doesn't add up ({'; '.join(problems)})"] if problems else []


def check_balance_arithmetic(rows: list[dict]) -> list[str]:
    """On every line, previous balance + credit - debit = balance. A mismatch means the statement was edited."""
    if not rows or rows[0]["balance"] is None:
        return ["Bank statement has no balance column: balance check not done"]
    for prev, row in zip(rows, rows[1:], strict=False):
        expected = round(prev["balance"] + row["credit"] - row["debit"], 2)
        if abs(expected - row["balance"]) > 0.01:
            return [f"MAJOR document: bank statement balance doesn't add up on {row['date']} "
                    f"({row['description']}): expected {inr(expected)}, shows {inr(row['balance'])}"]
    return []


def _fingerprint(applicant_id: str, doc_type: str) -> str | None:
    try:
        return hashlib.sha256(find_document(applicant_id, doc_type)[1]).hexdigest()
    except FileNotFoundError:
        return None


def check_file_reuse(applicant_id: str) -> list[str]:
    """The same payslip / statement / KYC file (identical SHA-256 of the file) on an EARLIER application.
    The earlier applicant is not flagged, only the later copy."""
    mine = {t: _fingerprint(applicant_id, t) for t in DOCUMENTS}
    my_date = (get_loan_request(applicant_id) or {}).get("application_date", "9999")
    flags = []
    for other in list_applicants():
        if other == applicant_id:
            continue
        if (get_loan_request(other) or {}).get("application_date", "9999") > my_date:
            continue
        for t in DOCUMENTS:
            if mine[t] and mine[t] == _fingerprint(other, t):
                flags.append(f"MAJOR document: {t} file is an exact copy of {other}'s file")
    return flags


# ---------- affordability and eligibility ----------

def monthly_emi(principal: float, annual_rate: float, months: int) -> float:
    """Standard EMI formula (reducing balance)."""
    r = annual_rate / 12 / 100
    if r == 0:
        return principal / months
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def check_affordability(applicant_id: str, credit_report: dict, slip: dict, rules: dict) -> dict:
    """FOIR = (existing EMIs + new EMI) / salary actually credited, the largest affordable loan,
    and basic eligibility (time in job, minimum net income). Returns the numbers plus flags."""
    request = get_loan_request(applicant_id)
    credited = get_salary_credits(applicant_id, slip.get("employer"))["monthly_salary"]
    existing = float(credit_report.get("existing_emi_monthly") or 0)
    max_foir = rules["max_debt_to_income_ratio"]
    result = {"loan_amount": None, "tenure_months": None, "interest_rate": None, "purpose": None,
              "new_emi": None, "existing_emi": existing, "salary_credited": credited, "foir": None,
              "max_foir": max_foir, "max_loan": None, "flags": []}
    if not request:
        result["flags"].append("No loan request on file: affordability (FOIR) not checked")
    else:
        n, rate = request["tenure_months"], request["interest_rate"]
        new_emi = monthly_emi(request["loan_amount"], rate, n)
        room = max(0.0, max_foir * credited - existing)          # EMI the salary can still carry
        r = rate / 12 / 100
        max_loan = room * n if r == 0 else room * ((1 + r) ** n - 1) / (r * (1 + r) ** n)
        foir = (existing + new_emi) / credited if credited else float("inf")
        result.update({k: request.get(k) for k in ["loan_amount", "tenure_months", "interest_rate", "purpose"]})
        result.update({"new_emi": round(new_emi), "foir": round(foir, 3) if credited else None,
                       "max_loan": int(max_loan // 1000 * 1000)})
        if foir > max_foir:
            result["flags"].append(
                f"MAJOR affordability: FOIR {foir:.0%} is above the {max_foir:.0%} limit "
                f"(EMIs {inr(existing + new_emi)}/mo on salary {inr(credited)}/mo); "
                f"largest affordable loan about {inr(result['max_loan'])}")
    joined, paid = slip.get("date_of_joining"), slip.get("pay_date")
    if joined and paid:
        months = (int(paid[:4]) - int(joined[:4])) * 12 + int(paid[5:7]) - int(joined[5:7])
        result["job_tenure_months"] = months
        minimum = rules.get("min_job_tenure_months", 12)
        if months < minimum:
            result["flags"].append(f"Eligibility: only {months} months in current job (minimum {minimum})")
    net = slip.get("net_pay")
    if net is not None and net < rules.get("min_net_monthly_income", 0):
        result["flags"].append(f"Eligibility: net pay {inr(net)}/mo is below the minimum "
                               f"{inr(rules['min_net_monthly_income'])}")
    return result


# ---------- risk tier ----------

def calculate_risk_score(credit_report: dict, banking_history: dict, flags: list[str], rules: dict) -> str:
    """HIGH = a hard rule broken or a MAJOR flag; MEDIUM = any other flag or just outside LOW; else LOW."""
    score, loans = credit_report["credit_score"], credit_report["existing_loans"]
    has_default = credit_report["defaults"] > 0 or banking_history.get("past_loan_defaults", 0) > 0
    if has_default and rules["auto_reject_if_default_exists"]:
        return "HIGH"
    if score < rules["min_credit_score"] or loans > rules["max_existing_loans"]:
        return "HIGH"
    if any(f.startswith("MAJOR") for f in flags):
        return "HIGH"
    if flags:
        return "MEDIUM"
    if score >= rules.get("low_risk_min_credit_score", 700) and loans <= rules.get("low_risk_max_existing_loans", 1):
        return "LOW"
    return "MEDIUM"


# ---------- audit ----------

def log_audit_action(applicant_id: str, action: str, details: dict) -> dict:
    """One new object per event (never rewrites an old one): loan-docs/<id>/audit/<time>-<id>.json"""
    check_applicant_id(applicant_id)
    now = datetime.now(UTC)
    entry = {"applicant_id": applicant_id, "action": action, "details": details,
             "timestamp": now.isoformat(timespec="seconds")}
    key = f"loan-docs/{applicant_id}/audit/{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}.json"
    storage.put_text(key, json.dumps(entry, indent=2))
    return entry


def read_audit_entries(applicant_id: str | None = None) -> list[dict]:
    """Every audit entry, newest first. Also reads the notebooks' older single-file log.json."""
    prefix = f"loan-docs/{check_applicant_id(applicant_id)}/audit/" if applicant_id else "loan-docs/"
    entries = []
    for key in storage.list_keys(prefix):
        if "/audit/" not in key or not key.endswith(".json"):
            continue
        data = json.loads(storage.get_text(key))
        entries += data if isinstance(data, list) else [data]
    return sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)
