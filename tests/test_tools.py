"""The checks themselves, in plain code: planted cases are caught, normal applicants raise no false alarm."""
import json

import pytest

from copilot import storage
from copilot import tools as t

PLANTED = {"APP_1025": "salary slip doesn't add up", "APP_1026": "balance doesn't add up",
           "APP_1027": "exact copy of APP_1008", "APP_1028": "FOIR"}


def all_flags(app: str) -> list[str]:
    slip = t.parse_salary_certificate(t.get_document(app, "income"))
    report = json.loads(storage.get_text(f"loan-docs/{app}/credit-bureau/report.json"))
    rules = t.get_policy_rules()
    afford = t.check_affordability(app, report, slip, rules)
    return (t.check_consistency(slip["declared_income"], t.get_salary_credits(app, slip["employer"]), rules,
                                slip["net_pay"])
            + t.check_slip_arithmetic(slip) + t.check_balance_arithmetic(t.get_statement_rows(app))
            + t.check_file_reuse(app) + afford["flags"])


def test_29_applicants():
    apps = t.list_applicants()
    assert len(apps) == 29 and apps[0] == "APP_1000" and apps[-1] == "APP_1028"


@pytest.mark.parametrize("app,words", PLANTED.items())
def test_planted_case_is_caught(app, words):
    assert any(words in f and f.startswith("MAJOR") for f in all_flags(app))


def test_short_job_tenure_flagged():
    assert any("months in current job" in f for f in all_flags("APP_1028"))


def test_no_document_alarms_on_normal_applicants():
    alarms = {a: f for a in t.list_applicants() if a not in PLANTED
              for f in all_flags(a) if f.startswith("MAJOR document")}
    assert alarms == {}


def test_original_is_not_punished_for_a_later_copy():
    assert not any("copy" in f for f in all_flags("APP_1008"))


def test_foir_case():
    assert any("FOIR" in f for f in all_flags("APP_1009"))


def test_inr_and_emi():
    assert t.inr(1500000) == "Rs.15,00,000" and t.inr(999) == "Rs.999"
    assert round(t.monthly_emi(100000, 12, 12)) == 8885


@pytest.mark.parametrize("bad", ["APP_12", "app_1001", "APP_1001/../x", ""])
def test_applicant_id_is_validated(bad):
    with pytest.raises(ValueError):
        t.check_applicant_id(bad)


def test_storage_refuses_paths_outside_the_data_folder():
    with pytest.raises(ValueError):
        storage.get_text("../../etc/passwd")


def test_gpt_answer_with_wrong_shapes_is_ignored(monkeypatch):
    """GPT once returned employer as a dict; only well-shaped values may be used."""
    bad = json.dumps({"declared_income": {"x": 1}, "employer": {"name": "HDFC"}, "pan": ["ABCDE1234F"],
                      "earnings": "lots", "net_pay": "Rs.50,000"})
    monkeypatch.setattr(t, "call_llm", lambda *a, **k: bad)
    text = t.get_document("APP_1001", "income")
    plain = t.parse_salary_certificate(text)
    fields = t.extract_income_fields(text)
    assert fields["employer"] == plain["employer"] and fields["pan"] == plain["pan"]
    assert fields["declared_income"] == plain["declared_income"] and fields["net_pay"] == 50000.0


def test_older_text_documents_still_work(tmp_path, monkeypatch):
    """The notebooks' .txt / .csv documents give the same answers as the PDFs."""
    folder = tmp_path / "loan-docs" / "APP_1001"
    (folder / "income").mkdir(parents=True)
    (folder / "bank-statements").mkdir()
    (folder / "income" / "salary_certificate.txt").write_text(
        "Employee Name: Priya Nair\nEmployer: Cognizant\nPAN: HQTAR4257W\nDate of Joining: 2023-11-26\n"
        "Pay Date: 2026-06-26\nEARNINGS\nBasic: 45000\nHRA: 22500\nSpecial Allowance: 22500\nGross Pay: 90000\n"
        "DEDUCTIONS\nProvident Fund: 5400\nProfessional Tax: 200\nIncome Tax (TDS): 10600\nTotal Deductions: 16200\n"
        "Net Pay: 73800\n")
    (folder / "bank-statements" / "statement_6m.csv").write_text(
        "date,description,debit,credit,balance\n2026-01-01,OPENING BALANCE,,,88560.00\n"
        "2026-01-27,SALARY CREDIT COGNIZANT,,49500.00,138060.00\n2026-01-27,RENT DEBIT,18000.00,,120060.00\n")
    from_pdf = t.parse_salary_certificate(t.get_document("APP_1001", "income"))
    pdf_rows = t.get_statement_rows("APP_1001")[:3]
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    assert t.parse_salary_certificate(t.get_document("APP_1001", "income")) == from_pdf
    assert t.get_statement_rows("APP_1001") == pdf_rows


def test_a_broken_pdf_is_reported(tmp_path, monkeypatch):
    folder = tmp_path / "loan-docs" / "APP_1001" / "income"
    folder.mkdir(parents=True)
    (folder / "salary_slip.pdf").write_bytes(b"not a pdf")
    monkeypatch.setenv("LOCAL_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Could not read the PDF"):
        t.get_document("APP_1001", "income")
