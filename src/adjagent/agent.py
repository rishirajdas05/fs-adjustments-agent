"""Orchestrator for the manual-adjustments slice.

    load -> translate -> validate (rules) -> classify -> propose fixes
         -> re-validate fixes -> apply human overrides -> explain
         -> post -> verify invariants (self-correct) -> write outputs

Plain code controls the flow. The only model call is in explain.py, and it
cannot change any decision this module makes.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Callable

from . import __version__
from .explain import call_anthropic, llm_explanation, template_explanation
from .fixes import _entry_with, propose
from .fx import account_balances, tb_balance_check, translate_tb
from .loaders import Inputs, load_all, sha256_file
from .models import Account, Decision, Finding, JournalEntry, Severity, SourceRef, Status
from .rules import Context, check_batch, check_entry, compute_pretax_income

# Rules a human override can never waive: they would break double entry or traceability.
HARD_RULES = {"JE_UNBALANCED", "JE_ACCOUNT_NOT_IN_COA", "JE_NO_NET_EFFECT", "JE_TOO_FEW_LINES",
              "JE_NEGATIVE_AMOUNT", "JE_TWO_SIDED_LINE", "JE_HEADER_ACCOUNT", "JE_POSTS_TO_SUSPENSE"}


def classify(findings: list[Finding]) -> Status:
    sev = {f.severity for f in findings}
    if Severity.FATAL in sev:
        return Status.REJECTED
    if Severity.FIXABLE in sev:
        return Status.QUARANTINED
    if Severity.WARNING in sev:
        return Status.ACCEPTED_WITH_WARNINGS
    return Status.ACCEPTED


class Run:
    def __init__(self, input_dir: Path, policy_path: Path, overrides_path: Path | None = None,
                 llm: Callable[[str, str], str] | None = None, llm_label: str = ""):
        self.input_dir, self.policy_path, self.overrides_path = input_dir, policy_path, overrides_path
        self.policy = json.loads(policy_path.read_text())
        self.overrides = json.loads(overrides_path.read_text()) if overrides_path else {}
        self.llm, self.llm_label = llm, llm_label
        self.trace_events: list[dict] = []
        self.coa_changes: list[dict] = []

    def trace(self, ev: dict):
        ev = {"seq": len(self.trace_events) + 1, **ev}
        self.trace_events.append(json.loads(json.dumps(ev, default=str)))

    # ------------------------------------------------------------------
    def execute(self) -> dict:
        inp: Inputs = load_all(self.input_dir)
        hashes = dict(inp.input_hashes)
        hashes["policy.json"] = sha256_file(self.policy_path)
        if self.overrides_path:
            hashes["overrides.json"] = sha256_file(self.overrides_path)
        code = hashlib.sha256(b"".join(f.read_bytes() for f in sorted(Path(__file__).parent.glob("*.py"))))
        self.code_sha256 = code.hexdigest()
        self.run_id = hashlib.sha256((json.dumps(hashes, sort_keys=True) + self.code_sha256).encode()).hexdigest()[:16]
        self.trace({"event": "run_start", "run_id": self.run_id, "code_version": __version__,
                    "code_sha256": self.code_sha256, "input_sha256": hashes, "llm": self.llm_label or None})

        context_findings = list(inp.load_findings)
        context_findings += translate_tb(inp.tb, inp.coa, inp.fx, inp.functional_currency, self.policy)
        balances = account_balances(inp.tb)
        tb_check = tb_balance_check(inp.tb)
        if tb_check["net_in_functional_after_translation"] != 0:
            context_findings.append(Finding(
                "TB_UNBALANCED", Severity.WARNING, None,
                f"Pre-adjustment TB does not balance: debits minus credits is "
                f"{tb_check['net_in_functional_after_translation']:,.2f} USD after translation "
                f"({tb_check['net_if_amounts_summed_ignoring_currency']:,.2f} if currencies are summed as-is)",
                {"net_usd_debit_minus_credit": tb_check["net_in_functional_after_translation"],
                 "net_if_currency_ignored": tb_check["net_if_amounts_summed_ignoring_currency"]}))
        for f in context_findings:
            self.trace({"event": "context_finding", **f.to_dict()})

        flagged: dict[str, list[str]] = {}
        for f in context_findings:
            if f.code.startswith("TB_") and "code" in f.facts:
                flagged.setdefault(f.facts["code"], []).append(f.code)
        ctx = Context(inp.coa, balances, inp.tb, inp.fx, self.policy, flagged,
                      compute_pretax_income(inp.coa, balances))
        self.trace({"event": "context_built", "pretax_income": ctx.pretax_income,
                    "materiality_threshold": ctx.materiality})

        batch = check_batch(inp.entries)
        decisions: dict[str, Decision] = {}
        entries: dict[str, JournalEntry] = {}
        discarded_by_id: dict[str, list[str]] = {}
        for je in inp.entries:
            entries[je.id] = je
            findings = check_entry(je, ctx) + [b for b in batch if b.entry_id == je.id]
            status = classify(findings)
            fixes, discarded = ([], [])
            if status == Status.QUARANTINED:
                fixes, discarded = propose(je, findings, ctx)
            discarded_by_id[je.id] = discarded
            for f in findings:
                self.trace({"event": "finding", **f.to_dict()})
            for fx in fixes:
                self.trace({"event": "fix_proposed", "entry_id": je.id, "fix_id": fx.fix_id,
                            "confidence": fx.confidence, "revalidated": fx.revalidated,
                            "notes": fx.revalidation_notes})
            for d in discarded:
                self.trace({"event": "fix_discarded", "entry_id": je.id, "note": d})
            decisions[je.id] = Decision(je.id, status, findings, fixes, "", "")
            self.trace({"event": "classified", "entry_id": je.id, "status": status.value})

        self._apply_overrides(decisions, entries, ctx)

        for jid, d in decisions.items():
            je = entries[jid]
            if self.llm:
                d.explanation, d.explanation_source = llm_explanation(
                    je, d.status, d.findings, d.proposed_fixes, discarded_by_id[jid], self.llm, self.llm_label,
                    self.trace)
            else:
                d.explanation = template_explanation(je, d.status, d.findings, d.proposed_fixes,
                                                     discarded_by_id[jid])
                d.explanation_source = "template"
            if d.override:
                d.explanation += (f"\nHuman decision: {d.override['action']} by {d.override.get('approver')} "
                                  f"({d.override.get('reason')}). Result: {d.override.get('result')}.")

        post_tb, invariants = self._post_and_verify(decisions, entries, ctx, balances)
        return {"inputs": inp, "ctx": ctx, "decisions": decisions, "entries": entries,
                "context_findings": context_findings, "tb_check": tb_check, "post_tb": post_tb,
                "invariants": invariants, "discarded": discarded_by_id, "hashes": hashes}

    # ------------------------------------------------------------------
    def _apply_overrides(self, decisions, entries, ctx: Context):
        for jid, ov in self.overrides.items():
            if jid.startswith("_"):
                continue
            d = decisions.get(jid)
            rec = dict(ov)
            if d is None:
                self.trace({"event": "override_refused", "entry_id": jid, "why": "unknown entry"})
                continue
            if not ov.get("approver") or not ov.get("reason"):
                rec["result"] = "REFUSED: an override needs an approver and a reason"
                d.override = rec
                self.trace({"event": "override_refused", "entry_id": jid, "why": rec["result"]})
                continue
            action = ov["action"]
            ack = set(ov.get("acknowledge", []))
            if ack & HARD_RULES:
                rec["result"] = f"REFUSED: {sorted(ack & HARD_RULES)} cannot be waived by an override"
                d.override = rec
                self.trace({"event": "override_refused", "entry_id": jid, "why": rec["result"]})
                continue
            je = entries[jid]
            if action == "confirm_reject":
                d.status = Status.REJECTED
                rec["result"] = "REJECTED (confirmed)"
            elif action == "sign_off":
                if d.status != Status.ACCEPTED_WITH_WARNINGS:
                    rec["result"] = f"REFUSED: sign_off only applies to ACCEPTED_WITH_WARNINGS, entry is {d.status.value}"
                else:
                    d.status = Status.ACCEPTED
                    rec["result"] = "ACCEPTED (warnings signed off)"
            elif action == "apply_fix":
                fix = next((f for f in d.proposed_fixes if f.fix_id == ov.get("fix_id")), None)
                if fix is None:
                    rec["result"] = f"REFUSED: fix {ov.get('fix_id')} was not among the proposals"
                elif fix.lines is None:
                    d.status = Status.REJECTED
                    rec["result"] = "REJECTED (withdrawn by approver, pipeline handles it)"
                else:
                    if fix.requires_coa_change:
                        if not ov.get("approve_coa_change"):
                            rec["result"] = "REFUSED: fix needs a COA change and approve_coa_change is not true"
                            d.override = rec
                            self.trace({"event": "override_refused", "entry_id": jid, "why": rec["result"]})
                            continue
                        code = next(l["account"] for l in fix.lines if l["account"] not in ctx.coa)
                        parent = ov.get("coa_parent", code[:2] + "00")
                        p = ctx.coa[parent]
                        ctx.coa[code] = Account(code, ov.get("coa_name", f"New account {code}"), "Expense"
                                                if p.statement == "PL" else p.account_type, parent, p.statement,
                                                ov.get("coa_cf_category", ""), p.normal_balance,
                                                SourceRef("overrides.json", f"{jid}.coa_change"))
                        self.coa_changes.append({"code": code, "name": ctx.coa[code].name, "parent": parent,
                                                 "approver": ov["approver"], "via": jid})
                    new_je = _entry_with(je, fix.lines)
                    found = check_entry(new_je, ctx)
                    blocking = [f for f in found if f.severity in (Severity.FATAL, Severity.FIXABLE)
                                and f.code not in ack]
                    if blocking:
                        rec["result"] = f"REFUSED on re-validation: {[f.code for f in blocking]}"
                    else:
                        for ln in new_je.lines:
                            ln.source = SourceRef("overrides.json", f"{jid}->{fix.fix_id}:{ln.source.locator}")
                        entries[jid] = new_je
                        d.findings = found
                        d.status = Status.ACCEPTED_WITH_WARNINGS if any(
                            f.severity != Severity.INFO for f in found) else Status.ACCEPTED
                        rec["result"] = f"{d.status.value} with {fix.fix_id}"
            elif action == "accept_as_is":
                blocking = {f.code for f in d.findings if f.severity in (Severity.FATAL, Severity.FIXABLE)}
                if blocking - ack:
                    rec["result"] = f"REFUSED: blocking findings not acknowledged: {sorted(blocking - ack)}"
                else:
                    d.status = Status.ACCEPTED_WITH_WARNINGS
                    rec["result"] = "ACCEPTED_WITH_WARNINGS (judgment-call findings acknowledged)"
            else:
                rec["result"] = f"REFUSED: unknown action {action}"
            d.override = rec
            self.trace({"event": "override", "entry_id": jid, **rec})

    # ------------------------------------------------------------------
    def _post_and_verify(self, decisions, entries, ctx: Context, balances):
        postable = {Status.ACCEPTED, Status.ACCEPTED_WITH_WARNINGS}
        for pass_no in range(1, int(self.policy["max_self_correction_passes"]) + 1):
            posted = {jid: entries[jid] for jid, d in decisions.items() if d.status in postable}
            adj: dict[str, list] = {}
            for jid, je in posted.items():
                for l in je.lines:
                    adj.setdefault(l.account, []).append((jid, str(l.source), l.net))
            accounts = sorted(set(balances) | set(adj))
            rows = []
            for a in accounts:
                pre = balances.get(a, {}).get("usd_net", Decimal("0.00"))
                delta = sum((x[2] for x in adj.get(a, [])), Decimal("0.00"))
                rows.append({"account": a, "pre": pre, "delta": delta, "post": pre + delta,
                             "refs": [x[1] for x in adj.get(a, [])], "je_ids": sorted({x[0] for x in adj.get(a, [])}),
                             "tb_sources": balances.get(a, {}).get("sources", [])})

            # Independent verification: recompute from the decisions, not from `rows`.
            failures: dict[str, list[str]] = {}
            for jid, je in posted.items():
                if je.total_debit != je.total_credit:
                    failures.setdefault(jid, []).append("I1 posted entry does not balance")
                for l in je.lines:
                    if l.account not in ctx.coa:
                        failures.setdefault(jid, []).append(f"I4 posted line to unknown account {l.account}")
            pre_total = sum((r["pre"] for r in rows), Decimal("0.00"))
            post_total = sum((r["post"] for r in rows), Decimal("0.00"))
            recomputed = {a: sum((l.net for je in posted.values() for l in je.lines if l.account == a),
                                 Decimal("0.00")) for a in accounts}
            row_mismatch = [r["account"] for r in rows if r["delta"] != recomputed[r["account"]]]
            flips = []
            for r in rows:
                acct = ctx.coa.get(r["account"])
                if not acct or not r["delta"]:
                    continue
                sign = 1 if acct.normal_balance == "Debit" else -1
                if r["pre"] * sign >= 0 and r["post"] * sign < 0:
                    flips.append(r["account"])
            invariants = {
                "pass": pass_no,
                "I1_each_posted_entry_balances": not any("I1" in m for v in failures.values() for m in v),
                "I2_batch_does_not_change_tb_net": post_total - pre_total == 0,
                "I3_every_account_delta_equals_sum_of_posted_lines": not row_mismatch,
                "I4_every_posted_line_hits_a_coa_account": not any("I4" in m for v in failures.values() for m in v),
                "I5_every_adjustment_traces_to_a_source_line": all(r["refs"] or not r["delta"] for r in rows),
                "abnormal_balance_flips": flips,
                "pre_tb_net_usd": pre_total, "post_tb_net_usd": post_total,
            }
            self.trace({"event": "verify", **invariants, "failures": failures})
            if not failures and invariants["I2_batch_does_not_change_tb_net"] and not row_mismatch:
                for a in flips:  # not fatal, but surface it on every entry that caused it
                    for jid in sorted({x[0] for x in adj[a]}):
                        d = decisions[jid]
                        d.findings.append(Finding("POST_ABNORMAL_BALANCE", Severity.WARNING, jid,
                                                  f"After posting, {a} has a balance opposite to its normal side", {"account": a}))
                        if d.status == Status.ACCEPTED:
                            d.status = Status.ACCEPTED_WITH_WARNINGS
                for jid, je in posted.items():
                    decisions[jid].posted_lines = [{"account": l.account, "debit": str(l.debit),
                                                    "credit": str(l.credit), "source": str(l.source)} for l in je.lines]
                return rows, invariants
            # Self-correction: pull the offending entries out and try again.
            for jid, msgs in failures.items():
                decisions[jid].status = Status.QUARANTINED
                decisions[jid].findings.append(Finding("POST_INVARIANT_FAILED", Severity.FIXABLE, jid,
                                                       "Pulled back by the post-posting verifier", {"failures": msgs}))
                self.trace({"event": "self_correct", "entry_id": jid, "demoted_to": "QUARANTINED", "why": msgs})
            if not failures:
                raise RuntimeError(f"Batch-level invariant failed with no entry to blame: {invariants}")
        raise RuntimeError("Self-correction did not converge")


def make_llm():
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("--llm needs ANTHROPIC_API_KEY set")
    return call_anthropic, os.environ.get("ADJAGENT_MODEL", "claude-sonnet-5-5")
