"""Turn structured findings into plain English for a finance user.

Two paths:
  * template (default, offline, reproducible)
  * LLM (--llm): the model writes the prose, but the output has to pass
    guardrails before it is used. If it fails twice, the template is used and
    the failure is recorded in the trace.

The LLM never decides an entry's status. By the time it is called the status
is fixed, and the guardrails reject any text that contradicts it.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Callable

from .models import Decision, Finding, JournalEntry, ProposedFix, Severity, Status

STATUS_HEADLINE = {
    Status.ACCEPTED: "Accepted. Will post as submitted.",
    Status.ACCEPTED_WITH_WARNINGS: "Accepted, pending reviewer sign-off. Will post, but someone needs to "
                                   "acknowledge the points below.",
    Status.QUARANTINED: "Held for review. Not posted. It can post once one of the fixes below is approved.",
    Status.REJECTED: "Rejected. Not posted, and there is no safe automatic fix. The preparer needs to resubmit.",
}


def fmt(x) -> str:
    try:
        return f"{Decimal(str(x)):,.2f}"
    except (InvalidOperation, ValueError):
        return str(x)


def _sentence(f: Finding, je: JournalEntry) -> str:
    x = f.facts
    c = f.code
    if c == "JE_UNBALANCED":
        return (f"Debits total {fmt(x['total_debit'])} but credits total {fmt(x['total_credit'])}, "
                f"a gap of {fmt(x['difference'])}. An entry that does not balance would knock the whole "
                f"trial balance out, so it cannot post.")
    if c == "JE_ACCOUNT_NOT_IN_COA":
        return (f"It {x['side']}s account {x['account']} ({fmt(x['amount'])}, memo '{x['memo']}'), "
                f"but {x['account']} does not exist in the chart of accounts, so the system cannot tell which "
                f"line of the statements it belongs on.")
    if c == "JE_NO_NET_EFFECT":
        s = (f"It debits and credits the same account ({', '.join(x['accounts'])}) by {fmt(x['gross_amount'])}. "
             f"The two lines cancel, so posting it would change no balance while adding a "
             f"{fmt(x['gross_amount'])} entry to the audit trail that looks like a real settlement.")
        if x.get("possible_intents"):
            s += " It may have been meant as: " + "; or ".join(x["possible_intents"]) + "."
        return s
    if c == "JE_INTERCOMPANY_NO_COUNTERPARTY":
        return ("It touches the intercompany account but names no counterparty entity, so it cannot be "
                "matched against the other entity's books for elimination.")
    if c == "JE_FX_REVAL_MISMATCH":
        rc = x["recomputed"]
        parts = [f"{fmt(v)} from the {b.replace('_', ' ')} rate {x['rates'][b]}" for b, v in sorted(rc.items())]
        return (f"The posted revaluation is {fmt(x['posted'])}. Recomputing it from fx_rates.csv for "
                f"{fmt(x['local_balance'])} {x['currency']} at the period-end rate {x['period_end_rate']} gives "
                f"{' or '.join(parts)}. Neither matches, so the source calculation needs checking.")
    if c == "JE_FX_REVAL_DOUBLE_COUNT":
        return (f"The system already converts {x['currency']} balances at the period-end rate when it builds the "
                f"statements, so this entry would book the same gain a second time.")
    if c == "JE_FX_REVAL_NO_CLOSING_RATE":
        return f"There is no period-end rate for {x['currency']}, so the revaluation cannot be checked."
    if c == "JE_TOUCHES_FLAGGED_ACCOUNT":
        return (f"Account {x['account']} has an open data-quality issue in the trial balance "
                f"({', '.join(x['issues'])}). Check the entry still makes sense once that is resolved.")
    if c == "JE_MATERIAL":
        return (f"At {fmt(x['amount'])} it is above the materiality threshold of {fmt(x['threshold'])} "
                f"({x['basis']}), so a reviewer must sign it off before the statements are released.")
    if c == "JE_ACCOUNT_NO_TB_BALANCE":
        return f"Account {x['account']} has no balance in this period's TB yet. Fine, but worth knowing."
    if c == "JE_DATE_OUT_OF_PERIOD":
        return f"It is dated {x['date']}, outside {x['period']}."
    if c == "JE_DUPLICATE_ENTRY":
        return f"It has the same lines and amounts as {x['same_as']} and may be a double submission."
    if c == "JE_HEADER_ACCOUNT":
        return f"Account {x['account']} is a heading in the chart of accounts, not an account you can post to."
    return f.title + "."


def template_explanation(je: JournalEntry, status: Status, findings: list[Finding],
                         fixes: list[ProposedFix], discarded: list[str]) -> str:
    out = [f"**{je.id}: {je.description}**", STATUS_HEADLINE[status]]
    blocking = [f for f in findings if f.severity in (Severity.FATAL, Severity.FIXABLE)]
    warnings = [f for f in findings if f.severity == Severity.WARNING]
    if blocking:
        out.append("What is wrong:")
        out += [f"- {_sentence(f, je)}" for f in blocking]
    if warnings:
        out.append("Points for the reviewer:" if blocking else "Before sign-off, note:")
        out += [f"- {_sentence(f, je)}" for f in warnings]
    if fixes:
        out.append("Options (none are applied automatically; each has been re-checked against the same rules):")
        for fx in fixes:
            out.append(f"- {fx.fix_id} (confidence {fx.confidence:.0%}): {fx.summary}. {fx.rationale}")
    if discarded:
        out.append("Considered and ruled out:")
        out += [f"- {d}" for d in discarded]
    if status == Status.QUARANTINED:
        out.append("Next step: pick an option (or send a corrected entry) and record the approval in "
                   "overrides.json. The entry is re-validated before it posts.")
    elif status == Status.REJECTED:
        out.append("Next step: the preparer should confirm what the entry was meant to do and submit a new one.")
    if not blocking and not warnings:
        out.append("All checks passed: balanced, valid accounts, in period, below materiality.")
    return "\n".join(out)


# ---------------------------------------------------------------- LLM path

NUM_RE = re.compile(r"(?<![A-Za-z])\$?\d[\d,]*(?:\.\d+)?")
CONTRADICTIONS = {
    Status.QUARANTINED: ["has been posted", "was posted", "has been approved", "has been accepted",
                         "was accepted"],
    Status.REJECTED: ["has been posted", "was posted", "has been approved", "has been accepted", "was accepted"],
}


def _norm(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace("$", "").replace(",", "")).normalize()
    except InvalidOperation:
        return None


def allowed_numbers(je: JournalEntry, findings: list[Finding], fixes: list[ProposedFix],
                    extra: list[str]) -> set[Decimal]:
    blob = json.dumps({"je": [(l.account, str(l.debit), str(l.credit), l.memo) for l in je.lines],
                       "id": je.id, "date": je.date, "desc": je.description, "src": je.source_doc,
                       "findings": [f.to_dict() for f in findings],
                       "fixes": [(x.fix_id, x.summary, x.rationale, x.lines, x.confidence) for x in fixes],
                       "extra": extra}, default=str)
    nums = {_norm(m) for m in NUM_RE.findall(blob)}
    nums |= {(Decimal(int(round(x.confidence * 100)))).normalize() for x in fixes}
    return {n for n in nums if n is not None}


def guardrail_violations(text: str, status: Status, findings: list[Finding], codes_addressed: list[str],
                         allowed: set[Decimal]) -> list[str]:
    v = []
    for m in NUM_RE.findall(text):
        n = _norm(m)
        if n is not None and n >= 100 and n not in allowed:
            v.append(f"number {m} does not appear in the entry, findings or fixes")
    need = {f.code for f in findings if f.severity in (Severity.FATAL, Severity.FIXABLE)}
    missing = need - set(codes_addressed)
    if missing:
        v.append(f"did not address blocking findings: {sorted(missing)}")
    low = text.lower()
    for phrase in CONTRADICTIONS.get(status, []):
        if phrase in low and f"not {phrase}" not in low:
            v.append(f"text says '{phrase}' but status is {status.value}")
    return v


SYSTEM = ("You explain journal-entry validation results to a finance user (an accountant, not an engineer). "
          "You are given an entry, its FINAL status, and machine findings. Write 3-8 short sentences in plain "
          "English: what is wrong, why it matters for the statements, and what the user should do. "
          "Rules: do not change or second-guess the status; use only numbers that appear in the input; do not "
          "invent account codes; do not use em dashes. Reply with JSON only: "
          '{"explanation": "...", "codes_addressed": ["<finding codes you covered>"]}')


def call_anthropic(prompt: str, system: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ADJAGENT_MODEL", "claude-sonnet-5-5")
    body = json.dumps({"model": model, "max_tokens": 800, "system": system,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    return "".join(b.get("text", "") for b in data["content"])


def llm_explanation(je: JournalEntry, status: Status, findings: list[Finding], fixes: list[ProposedFix],
                    discarded: list[str], llm: Callable[[str, str], str], model_label: str,
                    trace: Callable[[dict], None]) -> tuple[str, str]:
    payload = {
        "entry": {"id": je.id, "description": je.description, "date": je.date, "source": je.source_doc,
                  "lines": [{"account": l.account, "debit": str(l.debit), "credit": str(l.credit), "memo": l.memo}
                            for l in je.lines]},
        "final_status": status.value,
        "findings": [f.to_dict() for f in findings],
        "proposed_fixes": [{"fix_id": x.fix_id, "summary": x.summary, "confidence": x.confidence,
                            "rationale": x.rationale} for x in fixes],
        "ruled_out": discarded,
    }
    allowed = allowed_numbers(je, findings, fixes, discarded)
    prompt = json.dumps(payload, indent=1)
    feedback = ""
    for attempt in (1, 2):
        try:
            raw = llm(prompt + feedback, SYSTEM)
            obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            text, codes = obj["explanation"], obj.get("codes_addressed", [])
            viol = guardrail_violations(text, status, findings, codes, allowed)
        except Exception as e:  # network, JSON, missing keys
            text, viol = "", [f"call/parse failed: {type(e).__name__}: {e}"]
        trace({"event": "llm_explanation_attempt", "entry_id": je.id, "attempt": attempt,
               "violations": viol, "accepted": not viol})
        if not viol:
            return f"**{je.id}: {je.description}**\n{STATUS_HEADLINE[status]}\n{text}", f"llm:{model_label}"
        feedback = ("\n\nYour previous answer was rejected for these reasons, fix them: " + "; ".join(viol))
    return (template_explanation(je, status, findings, fixes, discarded),
            "template (llm output failed guardrail twice)")
