"""Deterministic validation rules for manual journal entries.

These rules decide whether an entry can post. No LLM is involved. The LLM
layer (explain.py) only ever turns their output into plain English, and the
fix proposals it produces are re-run through these same rules.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import CENT, Account, Finding, JournalEntry, Severity, TBLine


@dataclass
class Context:
    coa: dict[str, Account]
    balances: dict[str, dict]          # account -> {"usd_net", "sources", "by_currency"}
    tb: list[TBLine]
    fx: dict[tuple[str, str], Decimal]
    policy: dict
    flagged_accounts: dict[str, list[str]]  # account -> TB/COA finding codes touching it
    pretax_income: Decimal

    @property
    def materiality(self) -> Decimal:
        return (abs(self.pretax_income) * Decimal(self.policy["materiality_pct_of_pretax_income"])).quantize(CENT)


def compute_pretax_income(coa: dict[str, Account], balances: dict[str, dict]) -> Decimal:
    """Credit-positive pre-tax income from the TB (excludes the 8xxx tax accounts)."""
    total = Decimal("0.00")
    for code, b in balances.items():
        a = coa.get(code)
        if a and a.statement == "PL" and not (code.startswith("8")):
            total += b["usd_net"]
    return -total


def _f(code, sev, je, title, refs=None, **facts) -> Finding:
    return Finding(code, sev, je.id if je else None, title, facts, refs or [])


def check_entry(je: JournalEntry, ctx: Context) -> list[Finding]:
    out: list[Finding] = []
    p = ctx.policy

    # 1. structure
    if len(je.lines) < 2:
        out.append(_f("JE_TOO_FEW_LINES", Severity.FATAL, je,
                      "An entry needs at least one debit line and one credit line", [str(je.source)],
                      line_count=len(je.lines)))
    for l in je.lines:
        if l.debit < 0 or l.credit < 0:
            out.append(_f("JE_NEGATIVE_AMOUNT", Severity.FIXABLE, je, "Negative amount on a line",
                          [str(l.source)], account=l.account, debit=l.debit, credit=l.credit))
        if l.debit > 0 and l.credit > 0:
            out.append(_f("JE_TWO_SIDED_LINE", Severity.FIXABLE, je, "A line carries both a debit and a credit",
                          [str(l.source)], account=l.account, debit=l.debit, credit=l.credit))
        if l.debit == 0 and l.credit == 0:
            out.append(_f("JE_ZERO_LINE", Severity.WARNING, je, "A line has no amount", [str(l.source)],
                          account=l.account))

    # 2. balance
    dr, cr = je.total_debit, je.total_credit
    if dr != cr:
        out.append(_f("JE_UNBALANCED", Severity.FIXABLE, je, "Debits do not equal credits",
                      [str(l.source) for l in je.lines], total_debit=dr, total_credit=cr,
                      difference=(dr - cr).copy_abs(), heavier_side="debit" if dr > cr else "credit"))

    # 3. accounts exist and are postable
    for l in je.lines:
        a = ctx.coa.get(l.account)
        if a is None:
            out.append(_f("JE_ACCOUNT_NOT_IN_COA", Severity.FIXABLE, je,
                          f"Account {l.account} does not exist in the chart of accounts", [str(l.source)],
                          account=l.account, memo=l.memo, amount=l.debit or l.credit,
                          side="debit" if l.debit else "credit"))
        elif not a.postable:
            out.append(_f("JE_HEADER_ACCOUNT", Severity.FIXABLE, je,
                          f"Account {l.account} is a header and cannot be posted to", [str(l.source)],
                          account=l.account))
        elif l.account in p.get("never_plug_to", []):
            out.append(_f("JE_POSTS_TO_SUSPENSE", Severity.FIXABLE, je,
                          f"Entry posts to suspense account {l.account}", [str(l.source)], account=l.account))
        elif l.account not in ctx.balances:
            out.append(_f("JE_ACCOUNT_NO_TB_BALANCE", Severity.INFO, je,
                          f"Account {l.account} has no balance in the current TB", [str(l.source)],
                          account=l.account))

    # 4. date within period
    try:
        d = date.fromisoformat(je.date)
        if not (date.fromisoformat(p["period_start"]) <= d <= date.fromisoformat(p["period_end"])):
            out.append(_f("JE_DATE_OUT_OF_PERIOD", Severity.FIXABLE, je, "Entry date is outside the period",
                          [str(je.source)], date=je.date, period=p["period"]))
    except ValueError:
        out.append(_f("JE_BAD_DATE", Severity.FIXABLE, je, "Entry date is not a valid ISO date",
                      [str(je.source)], date=je.date))

    # 5. no economic effect / circular
    per_acct: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    sides: dict[str, set] = defaultdict(set)
    for l in je.lines:
        per_acct[l.account] += l.net
        if l.debit:
            sides[l.account].add("D")
        if l.credit:
            sides[l.account].add("C")
    gross = sum((l.debit + l.credit for l in je.lines), Decimal("0.00"))
    if gross > 0 and all(v == 0 for v in per_acct.values()):
        hyps = []
        if any(a in p.get("intercompany_accounts", []) for a in per_acct):
            acct = next(a for a in per_acct if a in p["intercompany_accounts"])
            hyps = [f"A cash settlement: debit {acct}, credit 1110 Cash for {dr:,.2f}",
                    "The UK subsidiary's side of the settlement (its IC receivable), which belongs in the UK "
                    "entity's books, not this one"]
        out.append(_f("JE_NO_NET_EFFECT", Severity.FATAL, je,
                      "Every account in the entry is debited and credited by the same amount, so it changes nothing",
                      [str(l.source) for l in je.lines], accounts=sorted(per_acct), gross_amount=dr,
                      possible_intents=hyps))
    else:
        for a, s in sides.items():
            if s == {"D", "C"}:
                out.append(_f("JE_SAME_ACCOUNT_BOTH_SIDES", Severity.WARNING, je,
                              f"Account {a} is both debited and credited", [], account=a, net=per_acct[a]))

    # 6. intercompany without a counterparty
    ic = [l for l in je.lines if l.account in p.get("intercompany_accounts", [])]
    if ic:
        out.append(_f("JE_INTERCOMPANY_NO_COUNTERPARTY", Severity.WARNING, je,
                      "Intercompany entry has no counterparty entity or matching IC receivable, so it cannot be "
                      "paired for elimination", [str(l.source) for l in ic],
                      accounts=sorted({l.account for l in ic})))

    # 7. FX revaluation recomputation
    text = f"{je.description} {' '.join(l.memo for l in je.lines)}".lower()
    is_reval = any(k in text for k in p["fx_reval_keywords"]) or (
        any(l.account in p["fx_reval_gain_loss_accounts"] for l in je.lines)
        and any(ctx.coa.get(l.account) and ctx.coa[l.account].statement == "BS" for l in je.lines))
    if is_reval:
        out.extend(_check_fx_reval(je, ctx, text))

    # 8. touches accounts with open data-quality issues
    for a in sorted({l.account for l in je.lines}):
        if a in ctx.flagged_accounts:
            out.append(_f("JE_TOUCHES_FLAGGED_ACCOUNT", Severity.WARNING, je,
                          f"Account {a} has an open data-quality issue in the TB "
                          f"({', '.join(ctx.flagged_accounts[a])})", [], account=a,
                          issues=ctx.flagged_accounts[a]))

    # 9. reclass across statements
    if "reclass" in je.description.lower():
        stmts = {ctx.coa[l.account].statement for l in je.lines if l.account in ctx.coa}
        if len(stmts) > 1:
            out.append(_f("JE_RECLASS_CROSS_STATEMENT", Severity.WARNING, je,
                          "Described as a reclass but moves value between the balance sheet and P&L", []))

    # 10. materiality
    if max(dr, cr) >= ctx.materiality:
        out.append(_f("JE_MATERIAL", Severity.WARNING, je,
                      "Entry is above the materiality threshold and needs reviewer sign-off", [],
                      amount=max(dr, cr), threshold=ctx.materiality,
                      basis=f"{Decimal(p['materiality_pct_of_pretax_income']) * 100:.0f}% of pre-tax income "
                            f"{ctx.pretax_income:,.2f}"))
    return out


_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")


def _check_fx_reval(je: JournalEntry, ctx: Context, text: str) -> list[Finding]:
    out = []
    p = ctx.policy
    tol = Decimal(p["fx_reval_tolerance"])
    bs_lines = [l for l in je.lines if l.account in ctx.coa and ctx.coa[l.account].statement == "BS"]
    named = {m for m in _CURRENCY_RE.findall(f"{je.description} {' '.join(l.memo for l in je.lines)}")}
    for l in bs_lines:
        bal = ctx.balances.get(l.account, {}).get("by_currency", {})
        currencies = [c for c in bal if c != "USD" and (not named or c in named)]
        for cur in currencies:
            local = bal[cur]
            end = ctx.fx.get((cur, "period_end"))
            if end is None:
                out.append(_f("JE_FX_REVAL_NO_CLOSING_RATE", Severity.FIXABLE, je,
                              f"Cannot verify revaluation of {cur}: no period-end rate", [str(l.source)],
                              currency=cur))
                continue
            candidates = {}
            for basis in ("period_average", "opening"):
                r = ctx.fx.get((cur, basis))
                if r is not None:
                    candidates[basis] = (local * (end - r)).quantize(CENT)
            posted = l.net.copy_abs()
            matched = [b for b, v in candidates.items() if (v.copy_abs() - posted).copy_abs() <= tol]
            if not matched:
                out.append(_f("JE_FX_REVAL_MISMATCH", Severity.FIXABLE, je,
                              f"Revaluation amount does not match any recomputation from fx_rates.csv",
                              [str(l.source)] + ctx.balances[l.account]["sources"],
                              currency=cur, local_balance=local, period_end_rate=end, posted=posted,
                              recomputed={b: str(v) for b, v in candidates.items()},
                              rates={b: str(ctx.fx[(cur, b)]) for b in candidates}))
            if p.get("system_remeasures_monetary_fx"):
                out.append(_f("JE_FX_REVAL_DOUBLE_COUNT", Severity.FIXABLE, je,
                              f"The pipeline already translates {cur} balances at the period-end rate, so a manual "
                              f"revaluation would be counted twice", [str(l.source)], currency=cur,
                              account=l.account))
    return out


def check_batch(entries: list[JournalEntry]) -> list[Finding]:
    """Cross-entry checks: exact duplicate entries."""
    out = []
    sigs = defaultdict(list)
    for je in entries:
        sig = tuple(sorted((l.account, str(l.debit), str(l.credit)) for l in je.lines))
        sigs[sig].append(je.id)
    for sig, ids in sigs.items():
        if len(ids) > 1:
            for i in ids[1:]:
                out.append(Finding("JE_DUPLICATE_ENTRY", Severity.FIXABLE, i,
                                   f"Same lines and amounts as {ids[0]}; possible double submission", {"same_as": ids[0]}))
    return out
