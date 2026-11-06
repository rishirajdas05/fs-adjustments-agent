import json
from decimal import Decimal
from pathlib import Path

import pytest

from adjagent import agent as agent_mod
from adjagent.agent import Run
from adjagent.models import Status
from adjagent.outputs import write_all

ROOT = Path(__file__).resolve().parents[1]
INPUTS, POLICY = ROOT / "inputs", ROOT / "config" / "policy.json"


def run(overrides=None, llm=None, label=""):
    r = Run(INPUTS, POLICY, overrides, llm, label)
    return r, r.execute()


def test_each_seeded_entry_gets_the_expected_decision():
    _, res = run()
    got = {k: d.status for k, d in res["decisions"].items()}
    assert got == {
        "JE-001": Status.ACCEPTED_WITH_WARNINGS,  # material
        "JE-002": Status.QUARANTINED,             # unbalanced
        "JE-003": Status.QUARANTINED,             # FX reval mismatch + double count
        "JE-004": Status.ACCEPTED,
        "JE-005": Status.QUARANTINED,             # 6315 not in COA
        "JE-006": Status.ACCEPTED,
        "JE-007": Status.ACCEPTED,
        "JE-008": Status.REJECTED,                # circular IC, no effect
        "JE-009": Status.ACCEPTED,
        "JE-010": Status.ACCEPTED,
    }


def test_unbalanced_entry_gets_two_balanced_revalidated_options_and_no_suspense_plug():
    _, res = run()
    d = res["decisions"]["JE-002"]
    assert len(d.proposed_fixes) == 2
    for fx in d.proposed_fixes:
        assert fx.revalidated
        assert sum(Decimal(l["debit"]) for l in fx.lines) == sum(Decimal(l["credit"]) for l in fx.lines)
        assert all(l["account"] != "9999" for l in fx.lines)
    assert any("suspense" in n for n in res["discarded"]["JE-002"])


def test_nearest_account_is_discarded_when_it_would_make_the_entry_circular():
    _, res = run()
    d = res["decisions"]["JE-005"]
    assert all("6310 Travel" not in fx.summary for fx in d.proposed_fixes)
    assert any("6310" in n and "JE_NO_NET_EFFECT" in n for n in res["discarded"]["JE-005"])
    assert d.proposed_fixes[0].requires_coa_change


def test_fx_reval_is_recomputed_from_rates_file():
    _, res = run()
    f = next(f for f in res["decisions"]["JE-003"].findings if f.code == "JE_FX_REVAL_MISMATCH")
    assert f.facts["recomputed"] == {"period_average": "10730.20", "opening": "19809.60"}


def test_gbp_fallback_is_flagged_not_silent():
    _, res = run()
    f = next(f for f in res["context_findings"] if f.code == "FX_RATE_MISSING")
    assert f.facts["currency"] == "GBP" and f.facts["used_rate"] == Decimal("1.264")


def test_hard_rules_cannot_be_waived(tmp_path):
    ov = tmp_path / "o.json"
    ov.write_text(json.dumps({"JE-002": {"action": "accept_as_is", "acknowledge": ["JE_UNBALANCED"],
                                         "approver": "x", "reason": "y"}}))
    _, res = run(ov)
    d = res["decisions"]["JE-002"]
    assert d.status == Status.QUARANTINED and d.override["result"].startswith("REFUSED")


def test_override_needs_approver_and_reason(tmp_path):
    ov = tmp_path / "o.json"
    ov.write_text(json.dumps({"JE-001": {"action": "sign_off"}}))
    _, res = run(ov)
    assert res["decisions"]["JE-001"].status == Status.ACCEPTED_WITH_WARNINGS


def test_demo_overrides_post_and_keep_invariants():
    _, res = run(ROOT / "config" / "overrides.example.json")
    dec = res["decisions"]
    assert dec["JE-002"].status in (Status.ACCEPTED, Status.ACCEPTED_WITH_WARNINGS)
    assert dec["JE-005"].status in (Status.ACCEPTED, Status.ACCEPTED_WITH_WARNINGS)
    assert dec["JE-008"].status == Status.REJECTED  # override refused
    assert all(v for k, v in res["invariants"].items() if k.startswith("I"))


def test_every_adjusted_account_traces_back_to_source_lines():
    _, res = run()
    for r in res["post_tb"]:
        if r["delta"]:
            assert r["refs"] and all(ref.startswith("manual_adjustments.json#entries[") for ref in r["refs"])


def test_verifier_catches_an_entry_that_slipped_past_the_rules(monkeypatch):
    real = agent_mod.check_entry
    monkeypatch.setattr(agent_mod, "check_entry",
                        lambda je, ctx: [] if je.id == "JE-002" else real(je, ctx))
    r, res = run()
    d = res["decisions"]["JE-002"]
    assert d.status == Status.QUARANTINED
    assert any(f.code == "POST_INVARIANT_FAILED" for f in d.findings)
    assert any(e["event"] == "self_correct" for e in r.trace_events)


def test_same_inputs_give_byte_identical_outputs(tmp_path):
    for name in ("a", "b"):
        r, res = run()
        res["code_version"] = "test"
        write_all(res, r, tmp_path / name)
    for f in (tmp_path / "a").iterdir():
        assert f.read_bytes() == (tmp_path / "b" / f.name).read_bytes(), f.name


def test_llm_output_with_invented_number_is_retried_then_falls_back():
    calls = []

    def lying_llm(prompt, system):
        calls.append(prompt)
        return json.dumps({"explanation": "Debits are 28,500 and credits 24,000.",
                           "codes_addressed": ["JE_UNBALANCED"]})

    r, res = run(llm=lying_llm, label="fake")
    d = res["decisions"]["JE-002"]
    assert d.explanation_source.startswith("template")
    assert "rejected for these reasons" in calls[-1]
    assert any(e["event"] == "llm_explanation_attempt" and not e["accepted"] for e in r.trace_events)


def test_llm_output_that_contradicts_status_is_rejected():
    def llm(prompt, system):
        return json.dumps({"explanation": "This entry has been posted.", "codes_addressed": ["JE_NO_NET_EFFECT"]})
    _, res = run(llm=llm, label="fake")
    assert res["decisions"]["JE-008"].explanation_source.startswith("template")


def test_good_llm_output_is_used():
    def llm(prompt, system):
        p = json.loads(prompt.split("\n\nYour previous")[0])
        codes = [f["code"] for f in p["findings"] if f["severity"] in ("FATAL", "FIXABLE")]
        return json.dumps({"explanation": "Plain explanation without figures.", "codes_addressed": codes})
    _, res = run(llm=llm, label="fake")
    assert all(d.explanation_source == "llm:fake" for d in res["decisions"].values())
