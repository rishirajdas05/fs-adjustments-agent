"""Generate candidate repairs for quarantined entries, then re-validate each one.

Candidates are generated deterministically. An LLM may re-rank them or write
the rationale (see explain.py), but it can never add a candidate that has not
passed the same rules engine a submitted entry has to pass.
Nothing here is ever applied automatically: a human picks one through an
override file.
"""
from __future__ import annotations

import copy
import re
from decimal import Decimal

from .models import Account, Finding, JELine, JournalEntry, ProposedFix, Severity, SourceRef
from .rules import Context, check_entry
from .explain import fmt

# Small, explicit synonym table. Expanding abbreviations is the sort of thing
# an LLM is good at; it is hard-coded here so the offline run is reproducible.
SYNONYMS = {"conf": "conference", "dep": "depreciation", "accum": "accumulated", "ic": "intercompany",
            "t&e": "travel entertainment", "ap": "payable", "ar": "receivable", "lt": "long-term"}
STOP = {"from", "to", "the", "of", "and", "out", "into", "old", "code", "reclass", "move", "reverse", "-", "&"}


def _tokens(s: str) -> set[str]:
    s = s.lower()
    for k, v in SYNONYMS.items():
        s = re.sub(rf"(?<![a-z]){re.escape(k)}(?![a-z])", v, s)
    return {t for t in re.findall(r"[a-z][a-z\-]+", s) if t not in STOP and len(t) > 2}


def _ordered_tokens(s: str) -> list[str]:
    """Like _tokens but keeps word order (sets are not stable across runs)."""
    out = []
    for word in s.split():
        for t in sorted(_tokens(word)):
            if t not in out:
                out.append(t)
    return out


def _lines_to_dicts(lines: list[JELine]) -> list[dict]:
    return [{"account": l.account, "debit": str(l.debit), "credit": str(l.credit), "memo": l.memo} for l in lines]


def _entry_with(je: JournalEntry, lines: list[dict]) -> JournalEntry:
    new = copy.deepcopy(je)
    new.lines = [JELine(d["account"], Decimal(d["debit"]), Decimal(d["credit"]), d.get("memo", ""),
                        SourceRef("proposed_fix", f"{je.id}.lines[{i}]")) for i, d in enumerate(lines)]
    return new


def revalidate(fix: ProposedFix, je: JournalEntry, ctx: Context, extra_accounts: dict[str, Account] | None = None):
    """Run the full rule set on the repaired entry. Only FATAL/FIXABLE block."""
    if fix.lines is None:
        fix.revalidated = True
        fix.revalidation_notes.append("Drop-entry fix: nothing to post, nothing to validate")
        return
    if extra_accounts:
        ctx = copy.copy(ctx)
        ctx.coa = {**ctx.coa, **extra_accounts}
    found = check_entry(_entry_with(je, fix.lines), ctx)
    blocking = [f for f in found if f.severity in (Severity.FATAL, Severity.FIXABLE)
                and f.code != "JE_FX_REVAL_DOUBLE_COUNT"]
    fix.revalidated = not blocking
    fix.revalidation_notes = [f"{f.code}: {f.title}" for f in blocking] or ["Passes all blocking rules"]


def propose(je: JournalEntry, findings: list[Finding], ctx: Context) -> tuple[list[ProposedFix], list[str]]:
    """Returns (fixes that survived re-validation, notes on candidates that were discarded)."""
    fixes: list[ProposedFix] = []
    discarded: list[str] = []
    codes = {f.code for f in findings}
    n = 0

    def new_id():
        nonlocal n
        n += 1
        return f"{je.id}-FIX-{chr(64 + n)}"

    # Unbalanced two-line entry: either side could be the typo. We do not pick.
    unbal = next((f for f in findings if f.code == "JE_UNBALANCED"), None)
    if unbal and len(je.lines) == 2:
        d_line = next(l for l in je.lines if l.debit)
        c_line = next(l for l in je.lines if l.credit)
        for target, amt in (("credit", d_line.debit), ("debit", c_line.credit)):
            lines = _lines_to_dicts(je.lines)
            for ld in lines:
                if target == "credit" and Decimal(ld["credit"]):
                    ld["credit"] = str(amt)
                if target == "debit" and Decimal(ld["debit"]):
                    ld["debit"] = str(amt)
            fixes.append(ProposedFix(new_id(), je.id,
                                     f"Make both sides {fmt(amt)} (change the {target} line)", 0.5, lines,
                                     f"A reclass moves one amount from one account to another, so both sides "
                                     f"should be equal. Nothing in the inputs says which figure is right; the "
                                     f"source document ('{je.source_doc}') has to settle it."))
    if unbal:
        discarded.append("Plugging the difference to suspense (9999) was not offered: it would hide the error "
                         "rather than fix it")

    # Account not in COA: rank existing accounts, plus the option of creating it.
    for f in findings:
        if f.code != "JE_ACCOUNT_NOT_IN_COA":
            continue
        bad = f.facts["account"]
        bad_line_idx = next(i for i, l in enumerate(je.lines) if l.account == bad)
        query = _tokens(f"{je.description} {je.lines[bad_line_idx].memo}")
        scored = []
        for a in ctx.coa.values():
            if not a.postable:
                continue
            name_sim = len(query & _tokens(a.name)) / max(1, len(query | _tokens(a.name)))
            code_sim = 1.0 if a.code[:3] == bad[:3] else 0.5 if a.code[:2] == bad[:2] else 0.0
            score = round(0.7 * name_sim + 0.3 * code_sim, 3)
            if score > 0.15:
                scored.append((score, a))
        scored.sort(key=lambda t: (-t[0], t[1].code))
        for score, a in scored[:4]:
            lines = _lines_to_dicts(je.lines)
            lines[bad_line_idx]["account"] = a.code
            fx_ = ProposedFix(new_id(), je.id, f"Post to existing account {a.code} {a.name} instead of {bad}",
                              score, lines, f"Name/memo similarity and code proximity to {bad}")
            revalidate(fx_, je, ctx)
            if fx_.revalidated:
                fixes.append(fx_)
            else:
                n -= 1  # reuse the letter
                discarded.append(f"Mapping {bad} to {a.code} {a.name} (score {score}) was discarded on "
                                 f"re-validation: {'; '.join(fx_.revalidation_notes)}")
        parent = next((c for c in (bad[:2] + "00", bad[:1] + "000") if c in ctx.coa
                       and not ctx.coa[c].postable), "")
        if parent:
            label = " ".join(w.capitalize() for w in _ordered_tokens(je.lines[bad_line_idx].memo)) or "New account"
            new_acct = Account(bad, label, "Expense" if bad[0] in "5678" else ctx.coa[parent].account_type,
                               parent, ctx.coa[parent].statement, "", ctx.coa[parent].normal_balance,
                               SourceRef("proposed_coa_change", bad))
            fx_ = ProposedFix(new_id(), je.id,
                              f"Keep {bad} and add it to the COA as '{label}' under {parent} "
                              f"{ctx.coa[parent].name}", 0.4, _lines_to_dicts(je.lines),
                              f"The description ('{je.description}') suggests {bad} is a deliberate new "
                              f"sub-account. That is a COA change and needs the COA owner's approval.",
                              requires_coa_change=True)
            revalidate(fx_, je, ctx, {bad: new_acct})
            (fixes if fx_.revalidated else discarded).append(fx_ if fx_.revalidated else
                                                             f"COA-change option failed: {fx_.revalidation_notes}")

    # FX revaluation: either drop it (pipeline remeasures) or restate at a recomputed amount.
    if "JE_FX_REVAL_DOUBLE_COUNT" in codes:
        fixes.append(ProposedFix(new_id(), je.id, "Withdraw the entry and let the pipeline remeasure the balance",
                                 0.6, None,
                                 "The translation step already values the foreign balance at the period-end rate. "
                                 "Posting this as well would book the gain twice. Right only if the TB carries "
                                 "this balance in transaction currency (see clarifying question 1)."))
    mism = next((f for f in findings if f.code == "JE_FX_REVAL_MISMATCH"), None)
    if mism:
        for basis, amt in sorted(mism.facts["recomputed"].items()):
            amt = Decimal(amt)
            lines = _lines_to_dicts(je.lines)
            for ld in lines:
                if Decimal(ld["debit"]):
                    ld["debit"] = str(amt.copy_abs())
                if Decimal(ld["credit"]):
                    ld["credit"] = str(amt.copy_abs())
            fx_ = ProposedFix(new_id(), je.id,
                              f"Restate to {fmt(amt.copy_abs())} ({basis.replace('_', ' ')} to period-end basis)",
                              0.25 if basis == "period_average" else 0.15, lines,
                              f"{fmt(mism.facts['local_balance'])} {mism.facts['currency']} x "
                              f"({mism.facts['period_end_rate']} - {mism.facts['rates'][basis]}). "
                              f"Right only if the ledger carries this balance at the {basis.replace('_', ' ')} rate.")
            revalidate(fx_, je, ctx)
            (fixes if fx_.revalidated else discarded).append(fx_ if fx_.revalidated else str(fx_.revalidation_notes))

    # Safety net: every surviving option must have passed the rules engine.
    survivors = []
    for fx_ in fixes:
        if not fx_.revalidated:
            revalidate(fx_, je, ctx)
        if fx_.revalidated:
            survivors.append(fx_)
        else:
            discarded.append(f"'{fx_.summary}' was discarded on re-validation: {'; '.join(fx_.revalidation_notes)}")
    fixes = survivors

    # Letter the options in the order a reviewer sees them (highest confidence first).
    fixes.sort(key=lambda x: (-x.confidence, x.fix_id))
    for i, fx_ in enumerate(fixes):
        fx_.fix_id = f"{je.id}-FIX-{chr(65 + i)}"
    return fixes, discarded
