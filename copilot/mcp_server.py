"""MCP tool server: the only door from the agents to documents, the bureau, the rules and the audit log.
It talks over stdin/stdout; the agents start it once with: python -m copilot.mcp_server"""
from mcp.server.fastmcp import FastMCP

from . import telemetry
from . import tools as t

mcp = FastMCP("loan-underwriting-tools", log_level="WARNING")


@mcp.tool()
def find_missing_documents(applicant_id: str) -> list[str]:
    """Required documents (salary slip, bank statement, KYC) missing for this applicant. Empty = all present."""
    return t.find_missing_documents(applicant_id)


@mcp.tool()
def read_salary_slip(applicant_id: str) -> dict:
    """Reads the payslip: gross (declared_income), net_pay, earnings, deductions, employer, name, pan, dates.
    llm_usage = the GPT calls it made (tokens, cost, time), which the agent takes off and adds to the case."""
    with telemetry.collect_llm_calls() as calls:
        fields = t.extract_income_fields(t.get_document(applicant_id, "income"))
    return {**fields, "llm_usage": calls}


@mcp.tool()
def get_credit_bureau_report(applicant_id: str, slip: dict | None = None) -> dict:
    """Checks KYC PAN = salary-slip PAN, then asks the credit bureau by PAN. Fails on a PAN mismatch."""
    return t.get_credit_bureau_report(applicant_id, slip)


@mcp.tool()
def get_core_banking_history(applicant_id: str) -> dict:
    """The applicant's history with our own bank (tenure, past loans, defaults), asked live by PAN. New to bank = all zero."""
    return t.get_core_banking_history(applicant_id)


@mcp.tool()
def get_policy_rules() -> dict:
    """The bank's lending rules from loan-policy/rules.json."""
    return t.get_policy_rules()


@mcp.tool()
def check_income(applicant_id: str, declared_income: float, employer: str | None = None,
                 net_pay: float | None = None) -> list[str]:
    """Salary actually credited in the bank statement vs NET pay on the slip (gross if no net). Returns flags."""
    salary = t.get_salary_credits(applicant_id, employer)
    return t.check_consistency(declared_income, salary, t.get_policy_rules(), net_pay)


@mcp.tool()
def check_documents(applicant_id: str, slip: dict) -> list[str]:
    """Payslip arithmetic, bank-statement running balance, and the same file reused from an earlier applicant."""
    return (t.check_slip_arithmetic(slip) + t.check_balance_arithmetic(t.get_statement_rows(applicant_id))
            + t.check_file_reuse(applicant_id))


@mcp.tool()
def check_affordability(applicant_id: str, credit_report: dict, slip: dict) -> dict:
    """FOIR (existing + new EMI vs salary credited), largest affordable loan, and basic eligibility."""
    return t.check_affordability(applicant_id, credit_report, slip, t.get_policy_rules())


@mcp.tool()
def calculate_risk_score(credit_report: dict, banking_history: dict, flags: list[str]) -> str:
    """LOW / MEDIUM / HIGH from the credit report, bank history, flags and policy rules."""
    return t.calculate_risk_score(credit_report, banking_history, flags, t.get_policy_rules())


@mcp.tool()
def log_audit_action(applicant_id: str, action: str, details: dict) -> dict:
    """Saves a permanent, timestamped record to the audit log."""
    return t.log_audit_action(applicant_id, action, details)


if __name__ == "__main__":
    mcp.run()
