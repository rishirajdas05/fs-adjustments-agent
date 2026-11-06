"""Core data types. Money is always Decimal, never float."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from typing import Any

CENT = Decimal("0.01")


def money(x: Any) -> Decimal:
    """Parse a value into a Decimal rounded to the cent. Raises on garbage."""
    if isinstance(x, Decimal):
        return x.quantize(CENT)
    if x is None or (isinstance(x, str) and x.strip() == ""):
        return Decimal("0.00")
    # str() first so 0.1 float does not become 0.1000000000000000055...
    return Decimal(str(x)).quantize(CENT)


@dataclass(frozen=True)
class SourceRef:
    """Points at the exact place in an input file a number came from."""
    file: str
    locator: str  # "row 49" for CSVs, "entries[1].lines[0]" for JSON

    def __str__(self) -> str:
        return f"{self.file}#{self.locator}"


@dataclass
class Account:
    code: str
    name: str
    account_type: str
    parent_code: str
    statement: str
    cf_category: str
    normal_balance: str
    source: SourceRef

    @property
    def postable(self) -> bool:
        return self.account_type != "Header"


@dataclass
class TBLine:
    account_code: str
    account_name: str
    currency: str
    debit: Decimal
    credit: Decimal
    source: SourceRef
    # filled by FX translation
    rate: Decimal | None = None
    rate_basis: str | None = None
    usd_net: Decimal | None = None  # debit-positive, in functional currency

    @property
    def local_net(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class JELine:
    account: str
    debit: Decimal
    credit: Decimal
    memo: str
    source: SourceRef

    @property
    def net(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class JournalEntry:
    id: str
    description: str
    date: str
    source_doc: str
    lines: list[JELine]
    source: SourceRef

    @property
    def total_debit(self) -> Decimal:
        return sum((l.debit for l in self.lines), Decimal("0.00"))

    @property
    def total_credit(self) -> Decimal:
        return sum((l.credit for l in self.lines), Decimal("0.00"))


class Severity(str, Enum):
    FATAL = "FATAL"        # cannot post, no safe repair -> REJECTED
    FIXABLE = "FIXABLE"    # cannot post as submitted, human can resolve -> QUARANTINED
    WARNING = "WARNING"    # posts, but a reviewer must acknowledge
    INFO = "INFO"          # context only


class Status(str, Enum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_WARNINGS = "ACCEPTED_WITH_WARNINGS"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


@dataclass
class Finding:
    code: str
    severity: Severity
    entry_id: str | None
    title: str
    facts: dict[str, Any]
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        d["facts"] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in self.facts.items()}
        return d


@dataclass
class ProposedFix:
    fix_id: str
    entry_id: str
    summary: str
    confidence: float  # 0..1, how likely this matches the preparer's intent
    lines: list[dict] | None  # replacement lines, or None for "drop the entry"
    rationale: str
    revalidated: bool = False
    revalidation_notes: list[str] = field(default_factory=list)
    requires_coa_change: bool = False


@dataclass
class Decision:
    entry_id: str
    status: Status
    findings: list[Finding]
    proposed_fixes: list[ProposedFix]
    explanation: str
    explanation_source: str  # "template" | "llm:<model>" | "template (llm output failed guardrail)"
    override: dict | None = None
    posted_lines: list[dict] = field(default_factory=list)
