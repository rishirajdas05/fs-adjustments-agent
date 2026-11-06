# Manual-adjustments agent (financial statements take-home)

A prototype of one slice of an agentic financial-reporting pipeline. It validates a batch of manual journal entries against a messy trial balance and chart of accounts, decides whether each entry is **accepted, quarantined or rejected**, proposes repairs that it has re-checked itself, explains every decision in plain English for a finance user, posts the accepted entries, and verifies the result before writing anything out.

| Document | What it is |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Architecture for the whole system (main deliverable); also as PDF |
| [`docs/REFLECTION.md`](docs/REFLECTION.md) | One-page reflection |
| [`docs/CLARIFYING_QUESTIONS.md`](docs/CLARIFYING_QUESTIONS.md) | Questions sent before starting, and the assumptions made in the meantime |
| [`output/`](output/) | Artifacts from a run on the supplied data |

## Setup and run

Python 3.10+. No runtime dependencies (standard library only); `pytest` for tests.

```bash
pip install pytest            # only needed for tests

# validate the batch, write artifacts to output/
PYTHONPATH=src python -m adjagent --inputs inputs --out output

# replay with human decisions (the demo file is clearly marked as invented)
PYTHONPATH=src python -m adjagent --overrides config/overrides.example.json --out output/with_overrides

# optional: Claude writes the explanations (guardrailed, falls back to templates)
export ANTHROPIC_API_KEY=...  # ADJAGENT_MODEL defaults to claude-sonnet-5-5
PYTHONPATH=src python -m adjagent --llm --out output/llm

python -m pytest -q           # 14 tests
```

`inputs/` holds the supplied files, **unchanged**.

## What comes out

| File | For whom | Contents |
|---|---|---|
| `adjustments_report.md` | Finance user | Decision per entry in plain English, options, effect on balances, post-posting checks, upstream data issues |
| `decisions.json` | Systems | Status, findings (code, severity, facts, source refs), proposed fixes with re-validation results, ruled-out options, override record, posted lines |
| `post_adjustment_tb.csv` | Auditor | Per account: before, adjustments, after, the JE lines that moved it, and the TB rows it came from |
| `review_queue.json` | Reviewer | What needs a human: fixes to choose, sign-offs, COA change requests, upstream issues |
| `trace.jsonl` | Auditor / debugging | Ordered log of every finding, proposal, discard, override, LLM attempt and verification pass |
| `run_manifest.json` | Audit / reproducibility | Run id, sha256 of every input, the policy file and the code; invariant results; counts |

## Results on the supplied data

| Entry | Decision | Reason |
|---|---|---|
| JE-001 | Accepted, needs sign-off | 850,000 is above materiality (5% of pre-tax income) |
| JE-002 | Quarantined | Debits 28,500 vs credits 25,000; two balanced options offered; no suspense plug |
| JE-003 | Quarantined | 11,200 matches no recomputation from the rate file; would double count the remeasurement |
| JE-005 | Quarantined | 6315 not in COA; nearest account (6310) discarded because it makes the entry circular |
| JE-008 | Rejected | Same account debited and credited 320,000: no effect, no IC counterparty |
| others | Accepted | |

The run also reports, but does not fix, the upstream issues:
- The TB is out by 182,460.20 USD after translation.
- 6310 appears twice in the TB.
- 9999 is an orphan account.
- GBP has no period-end rate.
- COA structure problems: 1150 and 2170 have cash-flow category TBD; 1290 is an empty header; 8000 and 3300 have children but are not headers; 7000 has no normal balance.

## How it is put together

```
src/adjagent/
  loaders.py   read inputs as-is, attach file#row provenance, raise load findings
  fx.py        deterministic translation, explicit and flagged fallbacks
  rules.py     the rules engine: decides FATAL / FIXABLE / WARNING / INFO
  fixes.py     candidate repairs, each re-run through rules.py before it is shown
  explain.py   template explanations; optional LLM path with guardrails
  agent.py     orchestration, human overrides, posting, invariant checks with self-correction
  outputs.py   deterministic writers
```

Design rules the code enforces:
- **The LLM never decides.** Status is fixed before any model call. Model output is rejected if it:
  - skips a blocking finding,
  - uses a number not in its input, or
  - contradicts the status.

  On rejection it retries once with the violations fed back, then falls back to the template.
- **Repairs are proposals.** Nothing is auto-applied. A human picks one in an overrides file, and it is re-validated before posting.
- **Some rules cannot be overridden:** balance, account existence, no-effect entries, suspense plugs.
- **The verifier is independent of the posting code.** A test sneaks an unbalanced entry past the rules, and the verifier catches it and quarantines it.
- **Idempotent.** Same inputs, policy, overrides and code give the same run id and byte-identical outputs (tested).

## Use of AI tools

## Use of AI tools

I used Claude as an AI pair programmer, as the brief encourages. It helped analyse the input data, write much of the code and tests, and draft the documentation.

My part: reviewing the accounting decisions (what is rejected vs held, which rules can never be overridden), choosing the prototype slice, writing the clarifying questions, running and testing the project on my own machine, and preparing the submission. The reflection describes where the AI helped and where its output had to be corrected.
