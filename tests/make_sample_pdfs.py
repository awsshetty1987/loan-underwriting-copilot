"""Turns the text test documents into PDFs, the way customers send them: payslip, bank statement and KYC.
Used once to build sample_data/ (run again only if you change the source text files):

    python tests/make_sample_pdfs.py sample_data

For each applicant it reads income/salary_certificate.txt, bank-statements/statement_6m.csv and kyc/id_proof.txt,
writes salary_slip.pdf, statement_6m.pdf and id_proof.pdf next to them, and deletes the text versions.
The PDFs are byte-for-byte reproducible, so a statement copied between applicants (APP_1027) stays an exact copy.
"""
import csv
import io
import sys
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

rl_config.invariant = 1                    # same input -> same bytes (no timestamps or random ids)
STYLES = getSampleStyleSheet()
GRID = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8ebf0")),
                   ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                   ("FONTSIZE", (0, 0), (-1, -1), 9),
                   ("ALIGN", (-1, 1), (-1, -1), "RIGHT")])
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def money(value) -> str:
    return "" if value in (None, "") else f"{float(value):,.2f}"


def build(path: Path, story: list):
    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                      bottomMargin=16 * mm, title=path.stem, author="Synthetic test data").build(story)
    path.write_bytes(buffer.getvalue())


def payslip(text: str, out: Path):
    fields, section, earnings, deductions = {}, None, [], []
    for line in text.splitlines():
        line = line.strip()
        if line.upper() in ("EARNINGS", "DEDUCTIONS"):
            section = line.upper()
            continue
        if ":" not in line:
            continue
        label, value = [p.strip() for p in line.split(":", 1)]
        if label in ("Gross Pay", "Total Deductions", "Net Pay", "Declared Monthly Income"):
            fields[label] = value
        elif section == "EARNINGS":
            earnings.append((label, value))
        elif section == "DEDUCTIONS":
            deductions.append((label, value))
        else:
            fields[label] = value
    if not earnings or "Gross Pay" not in fields:
        raise ValueError(f"{out}: payslip without an earnings breakdown")
    paid = fields.get("Pay Date", "")
    month = f"{MONTHS[int(paid[5:7]) - 1]} {paid[:4]}" if len(paid) >= 7 else ""
    story = [Paragraph(f"<b>{fields.get('Employer', '')}</b>", STYLES["Title"]),
             Paragraph(f"Salary slip for {month}", STYLES["Heading2"]),
             Paragraph("Synthetic test data, not real", STYLES["Italic"]), Spacer(1, 6 * mm)]
    for label in ["Employee Name", "Employer", "Designation", "PAN", "Date of Joining", "Pay Date"]:
        if label in fields:
            story.append(Paragraph(f"{label}: {fields[label]}", STYLES["Normal"]))
    story.append(Spacer(1, 6 * mm))
    for title, rows, total in [("EARNINGS", earnings, ("Gross Pay", fields["Gross Pay"])),
                               ("DEDUCTIONS", deductions, ("Total Deductions", fields.get("Total Deductions")))]:
        table = Table([[title, "Amount (Rs.)"]] + [[k, money(v)] for k, v in rows + [total]],
                      colWidths=[110 * mm, 50 * mm])
        table.setStyle(GRID)
        story += [table, Spacer(1, 5 * mm)]
    story.append(Paragraph(f"<b>Net Pay: {money(fields.get('Net Pay'))}</b>", STYLES["Heading3"]))
    build(out, story)


def statement(text: str, out: Path):
    rows = list(csv.DictReader(io.StringIO(text)))
    dates = sorted(r["date"] for r in rows)
    story = [Paragraph("<b>Synthetic Bank</b>", STYLES["Title"]),
             Paragraph(f"Account statement {dates[0]} to {dates[-1]}", STYLES["Heading2"]),
             Paragraph("Synthetic test data, not real", STYLES["Italic"]), Spacer(1, 6 * mm)]
    data = [["Date", "Description", "Debit", "Credit", "Balance"]]
    data += [[r["date"], r["description"], money(r["debit"]), money(r["credit"]), money(r["balance"])] for r in rows]
    table = LongTable(data, colWidths=[24 * mm, 70 * mm, 24 * mm, 24 * mm, 30 * mm], repeatRows=1)
    table.setStyle(TableStyle(GRID.getCommands() + [("ALIGN", (2, 1), (-1, -1), "RIGHT")]))
    story.append(table)
    build(out, story)


def kyc(text: str, out: Path):
    story = [Paragraph("<b>KYC details</b>", STYLES["Title"]),
             Paragraph("Synthetic test data, not real", STYLES["Italic"]), Spacer(1, 6 * mm)]
    story += [Paragraph(line.strip(), STYLES["Normal"]) for line in text.splitlines() if ":" in line]
    build(out, story)


def main(root: Path):
    for folder in sorted((root / "loan-docs").glob("APP_*")):
        for src, dst, make in [("income/salary_certificate.txt", "income/salary_slip.pdf", payslip),
                               ("bank-statements/statement_6m.csv", "bank-statements/statement_6m.pdf", statement),
                               ("kyc/id_proof.txt", "kyc/id_proof.pdf", kyc)]:
            if (folder / src).exists():
                make((folder / src).read_text(encoding="utf-8"), folder / dst)
                (folder / src).unlink()
        print(folder.name, "done")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "sample_data"))
