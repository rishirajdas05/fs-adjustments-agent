"""Write run artifacts. Contents are deterministic for a given set of inputs,
so two runs on the same inputs produce byte-identical files (idempotency)."""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from .explain import fmt
from .models import Severity, Status


def _j(obj) -> str:
    return json.dumps(obj, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else o.__dict__)


def write_all(res: dict, run, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    inp, ctx, decisions = res["inputs"], res["ctx"], res["decisions"]

    # 1. decisions.json
    dec = []
    for jid, d in decisions.items():
        dec.append({
            "entry_id": jid, "status": d.status.value,
            "findings": [f.to_dict() for f in d.findings],
            "proposed_fixes": [{k: v for k, v in fx.__dict__.items()} for fx in d.proposed_fixes],
            "ruled_out": res["discarded"][jid],
            "explanation": d.explanation, "explanation_source": d.explanation_source,
            "override": d.override, "posted_lines": d.posted_lines,
        })
    (out / "decisions.json").write_text(_j(dec) + "\n")

    # 2. post-adjustment TB with lineage
    with (out / "post_adjustment_tb.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["account_code", "account_name", "statement", "normal_balance", "pre_adjustment_usd",
                    "adjustments_usd", "post_adjustment_usd", "adjusting_entries", "adjustment_line_refs",
                    "tb_source_rows", "open_issues"])
        flagged = {}
        for f in res["context_findings"]:
            if "code" in f.facts and f.severity != Severity.INFO:
                flagged.setdefault(f.facts["code"], []).append(f.code)
        for r in res["post_tb"]:
            a = ctx.coa.get(r["account"])
            w.writerow([r["account"], a.name if a else "(not in COA)", a.statement if a else "",
                        a.normal_balance if a else "", r["pre"], r["delta"], r["post"], ";".join(r["je_ids"]),
                        ";".join(r["refs"]), ";".join(r["tb_sources"]), ";".join(flagged.get(r["account"], []))])

    # 3. human review queue
    queue = []
    for jid, d in decisions.items():
        if d.status == Status.QUARANTINED:
            queue.append({"entry_id": jid, "needs": "choose a fix or send a corrected entry",
                          "options": [fx.fix_id for fx in d.proposed_fixes]})
        elif d.status == Status.REJECTED and not (d.override and d.override.get("result", "").startswith("REJECTED")):
            queue.append({"entry_id": jid, "needs": "preparer to confirm intent and resubmit"})
        elif d.status == Status.ACCEPTED_WITH_WARNINGS:
            queue.append({"entry_id": jid, "needs": "reviewer sign-off",
                          "warnings": [f.code for f in d.findings if f.severity == Severity.WARNING]})
    upstream = [f.to_dict() for f in res["context_findings"] if f.severity != Severity.INFO]
    (out / "review_queue.json").write_text(_j({"entries": queue, "upstream_data_issues": upstream,
                                               "coa_change_requests": run.coa_changes}) + "\n")

    # 4. trace
    with (out / "trace.jsonl").open("w") as fh:
        for ev in run.trace_events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")

    # 5. manifest
    (out / "run_manifest.json").write_text(_j({
        "run_id": run.run_id, "code_version": res.get("code_version"), "code_sha256": run.code_sha256, "period": inp.period,
        "functional_currency": inp.functional_currency, "input_sha256": res["hashes"],
        "policy_version": run.policy["policy_version"], "explainer": run.llm_label or "template",
        "invariants": res["invariants"],
        "counts": {s.value: sum(1 for d in decisions.values() if d.status == s) for s in Status},
    }) + "\n")

    # 6. finance-user report
    (out / "adjustments_report.md").write_text(render_report(res, run))


def render_report(res, run) -> str:
    inp, ctx, decisions, entries = res["inputs"], res["ctx"], res["decisions"], res["entries"]
    L = [f"# Manual adjustments review: {inp.period}", "",
         f"Run `{run.run_id}` | functional currency {inp.functional_currency} | "
         f"materiality threshold {fmt(ctx.materiality)} (5% of pre-tax income {fmt(ctx.pretax_income)}) | "
         f"explanations: {run.llm_label or 'template'}", "",
         "## Summary", "", "| Entry | Description | Amount | Decision |", "|---|---|---:|---|"]
    orig = {je.id: je for je in inp.entries}
    for jid, d in decisions.items():
        je = orig[jid]
        L.append(f"| {jid} | {je.description} | {fmt(max(je.total_debit, je.total_credit))} | "
                 f"{d.status.value.replace('_', ' ').title()} |")
    counts = {s: sum(1 for d in decisions.values() if d.status == s) for s in Status}
    L += ["", " | ".join(f"{s.value.replace('_', ' ').title()}: {n}" for s, n in counts.items()), "",
          "## Entry by entry", ""]
    for jid, d in decisions.items():
        L += [d.explanation.replace("\n- ", "\n  - ").replace("\n", "\n\n").replace("\n\n  - ", "\n  - "), ""]
    L += ["## Effect of the posted entries", "",
          "| Account | Name | Before (USD, Dr+) | Adjustments | After | Entries |", "|---|---|---:|---:|---:|---|"]
    for r in res["post_tb"]:
        if r["delta"]:
            a = ctx.coa.get(r["account"])
            L.append(f"| {r['account']} | {a.name if a else '?'} | {fmt(r['pre'])} | {fmt(r['delta'])} | "
                     f"{fmt(r['post'])} | {', '.join(r['je_ids'])} |")
    inv = res["invariants"]
    L += ["", "## Checks run after posting", ""]
    for k, v in inv.items():
        if k.startswith("I"):
            L.append(f"- {'PASS' if v else 'FAIL'}: {k[3:].replace('_', ' ')}")
    L.append(f"- Abnormal balance flips: {', '.join(inv['abnormal_balance_flips']) or 'none'}")
    L += [f"- TB net (debits minus credits, USD) before: {fmt(inv['pre_tb_net_usd'])}, after: "
          f"{fmt(inv['post_tb_net_usd'])}. The adjustments do not change it; the starting imbalance is an "
          f"upstream problem listed below.", "",
          "## Upstream data issues this run worked around (not fixed here)", "",
          "These belong to the TB ingestion and COA mapping stages. They are listed because they affect how "
          "far the adjusted numbers can be trusted.", ""]
    for f in res["context_findings"]:
        if f.severity != Severity.INFO:
            L.append(f"- **{f.code}**: {f.title}" + (f" ({', '.join(f.refs)})" if f.refs else ""))
    info = [f for f in res["context_findings"] if f.severity == Severity.INFO]
    if info:
        L += ["", "Informational:", ""] + [f"- {f.code}: {f.title}" for f in info]
    L.append("")
    return "\n".join(L)
