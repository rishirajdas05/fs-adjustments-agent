"""Deterministic FX translation of TB rows into the functional currency.

No LLM is involved. Every translated row records the rate it used and why,
and any fallback is raised as a finding instead of being applied silently.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .models import CENT, Account, Finding, Severity, TBLine


def translate_tb(tb: list[TBLine], coa: dict[str, Account], fx: dict[tuple[str, str], Decimal],
                 functional: str, policy: dict) -> list[Finding]:
    findings: list[Finding] = []
    fallback_reported: set[tuple[str, str]] = set()
    for line in tb:
        if line.currency == functional:
            line.rate, line.rate_basis = Decimal("1"), "functional"
            line.usd_net = line.local_net
            continue
        acct = coa.get(line.account_code)
        statement = acct.statement if acct else "BS"
        wanted = policy["bs_rate"] if statement == "BS" else policy["pl_rate"]
        rate = fx.get((line.currency, wanted))
        basis = wanted
        if rate is None:
            for alt in policy["fallback_order"]:
                if (line.currency, alt) in fx:
                    rate, basis = fx[(line.currency, alt)], f"{alt} (FALLBACK: {wanted} missing)"
                    break
            key = (line.currency, wanted)
            if key not in fallback_reported:
                fallback_reported.add(key)
                findings.append(Finding(
                    "FX_RATE_MISSING", Severity.WARNING, None,
                    f"No {wanted} rate for {line.currency}; "
                    + (f"used {basis.split(' ')[0]} {rate} as a flagged fallback" if rate else "row left untranslated"),
                    {"currency": line.currency, "wanted": wanted, "used_rate": rate}, [str(line.source)]))
        if rate is None:
            line.rate, line.rate_basis, line.usd_net = None, "UNTRANSLATED", None
            continue
        line.rate, line.rate_basis = rate, basis
        line.usd_net = (line.local_net * rate).quantize(CENT)
    return findings


def tb_balance_check(tb: list[TBLine]) -> dict:
    local = sum((l.local_net for l in tb), Decimal("0.00"))
    usd = sum((l.usd_net for l in tb if l.usd_net is not None), Decimal("0.00"))
    return {
        "net_if_amounts_summed_ignoring_currency": local,
        "net_in_functional_after_translation": usd,
        "untranslated_rows": [str(l.source) for l in tb if l.usd_net is None],
    }


def account_balances(tb: list[TBLine]) -> dict[str, dict]:
    out: dict[str, dict] = defaultdict(lambda: {"usd_net": Decimal("0.00"), "sources": [], "by_currency": {}})
    for l in tb:
        b = out[l.account_code]
        if l.usd_net is not None:
            b["usd_net"] += l.usd_net
        b["sources"].append(str(l.source))
        b["by_currency"][l.currency] = b["by_currency"].get(l.currency, Decimal("0.00")) + l.local_net
    return dict(out)
