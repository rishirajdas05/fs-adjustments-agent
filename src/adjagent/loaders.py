"""Load the raw inputs exactly as given, attaching provenance to every value.

Nothing is cleaned here. Problems found while loading become Findings, so the
messy data stays visible all the way to the report.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Account, Finding, JELine, JournalEntry, Severity, SourceRef, TBLine, money


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Inputs:
    coa: dict[str, Account]
    tb: list[TBLine]
    fx: dict[tuple[str, str], Decimal]
    entries: list[JournalEntry]
    period: str
    functional_currency: str
    input_hashes: dict[str, str]
    load_findings: list[Finding] = field(default_factory=list)


def _csv_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        # data rows start at line 2 (line 1 is the header)
        for i, row in enumerate(csv.DictReader(fh), start=2):
            yield i, row


def load_coa(path: Path, findings: list[Finding]) -> dict[str, Account]:
    coa: dict[str, Account] = {}
    for line_no, r in _csv_rows(path):
        ref = SourceRef(path.name, f"row {line_no}")
        code = r["account_code"].strip()
        if code in coa:
            findings.append(Finding("COA_DUPLICATE_CODE", Severity.WARNING, None,
                                    f"COA code {code} defined twice", {"code": code}, [str(ref)]))
            continue
        coa[code] = Account(code, r["account_name"].strip(), r["account_type"].strip(),
                            r["parent_code"].strip(), r["statement"].strip(),
                            r["cf_category"].strip(), r["normal_balance"].strip(), ref)

    children = defaultdict(list)
    for a in coa.values():
        if a.parent_code:
            children[a.parent_code].append(a.code)
    for a in coa.values():
        if a.parent_code and a.parent_code not in coa:
            findings.append(Finding("COA_MISSING_PARENT", Severity.WARNING, None,
                                    f"{a.code} points at parent {a.parent_code}, which is not in the COA",
                                    {"code": a.code, "parent": a.parent_code}, [str(a.source)]))
        if a.account_type == "Header" and not children[a.code]:
            findings.append(Finding("COA_EMPTY_HEADER", Severity.INFO, None,
                                    f"Header {a.code} {a.name} has no child accounts",
                                    {"code": a.code}, [str(a.source)]))
        if a.account_type != "Header" and children[a.code]:
            findings.append(Finding("COA_POSTABLE_PARENT", Severity.INFO, None,
                                    f"{a.code} {a.name} is a postable {a.account_type} account but also has children "
                                    f"({', '.join(children[a.code])}); rollups could double count",
                                    {"code": a.code, "children": children[a.code]}, [str(a.source)]))
        if not a.normal_balance:
            findings.append(Finding("COA_NO_NORMAL_BALANCE", Severity.INFO, None,
                                    f"{a.code} {a.name} has no normal balance",
                                    {"code": a.code}, [str(a.source)]))
        if a.statement == "BS" and a.postable and a.cf_category in ("TBD", ""):
            sev = Severity.WARNING if a.cf_category == "TBD" else Severity.INFO
            findings.append(Finding("COA_CF_CATEGORY_UNSET", sev, None,
                                    f"{a.code} {a.name} has cash-flow category '{a.cf_category or 'blank'}'",
                                    {"code": a.code, "cf_category": a.cf_category}, [str(a.source)]))
    return coa


def load_tb(path: Path, coa: dict[str, Account], findings: list[Finding]) -> list[TBLine]:
    lines: list[TBLine] = []
    seen: dict[tuple[str, str], list[TBLine]] = defaultdict(list)
    for line_no, r in _csv_rows(path):
        ref = SourceRef(path.name, f"row {line_no}")
        try:
            dr, cr = money(r["debit"]), money(r["credit"])
        except InvalidOperation:
            findings.append(Finding("TB_UNPARSEABLE_AMOUNT", Severity.WARNING, None,
                                    f"Row {line_no} has an amount that is not a number", dict(r), [str(ref)]))
            continue
        tl = TBLine(r["account_code"].strip(), r["account_name"].strip(), r["currency"].strip().upper(), dr, cr, ref)
        lines.append(tl)
        seen[(tl.account_code, tl.currency)].append(tl)
        if tl.account_code not in coa:
            findings.append(Finding("TB_ORPHAN_ACCOUNT", Severity.WARNING, None,
                                    f"TB account {tl.account_code} '{tl.account_name}' is not in the COA",
                                    {"code": tl.account_code, "net": tl.local_net}, [str(ref)]))
        elif coa[tl.account_code].name != tl.account_name:
            findings.append(Finding("TB_NAME_MISMATCH", Severity.INFO, None,
                                    f"TB name '{tl.account_name}' differs from COA name '{coa[tl.account_code].name}'",
                                    {"code": tl.account_code}, [str(ref)]))
    for (code, cur), group in seen.items():
        if len(group) > 1:
            findings.append(Finding("TB_DUPLICATE_ACCOUNT_ROW", Severity.WARNING, None,
                                    f"Account {code} appears {len(group)} times in {cur}; could be a double export "
                                    f"or a legitimate split. Kept both rows, flagged for review",
                                    {"code": code, "currency": cur,
                                     "amounts": [str(g.local_net) for g in group]},
                                    [str(g.source) for g in group]))
    return lines


def load_fx(path: Path, findings: list[Finding]) -> dict[tuple[str, str], Decimal]:
    fx = {}
    for line_no, r in _csv_rows(path):
        fx[(r["currency"].strip().upper(), r["rate_type"].strip())] = Decimal(r["rate"].strip())
    return fx


def load_entries(path: Path, findings: list[Finding]) -> tuple[list[JournalEntry], str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = []
    ids = defaultdict(int)
    for ei, e in enumerate(data.get("entries", [])):
        eref = SourceRef(path.name, f"entries[{ei}]")
        lines = []
        for li, l in enumerate(e.get("lines", [])):
            lines.append(JELine(str(l.get("account", "")).strip(), money(l.get("debit")), money(l.get("credit")),
                                l.get("memo", ""), SourceRef(path.name, f"entries[{ei}].lines[{li}]")))
        je = JournalEntry(e.get("id", f"<missing id #{ei}>"), e.get("description", ""), e.get("date", ""),
                          e.get("source", ""), lines, eref)
        ids[je.id] += 1
        entries.append(je)
    for jid, n in ids.items():
        if n > 1:
            findings.append(Finding("JE_DUPLICATE_ID", Severity.WARNING, jid, f"Entry id {jid} used {n} times",
                                    {"count": n}))
    return entries, data.get("period", ""), data.get("functional_currency", "")


def load_all(input_dir: Path) -> Inputs:
    findings: list[Finding] = []
    files = ["chart_of_accounts.csv", "trial_balance.csv", "fx_rates.csv", "manual_adjustments.json"]
    hashes = {f: sha256_file(input_dir / f) for f in files}
    coa = load_coa(input_dir / "chart_of_accounts.csv", findings)
    tb = load_tb(input_dir / "trial_balance.csv", coa, findings)
    fx = load_fx(input_dir / "fx_rates.csv", findings)
    entries, period, func = load_entries(input_dir / "manual_adjustments.json", findings)
    return Inputs(coa, tb, fx, entries, period, func or "USD", hashes, findings)
