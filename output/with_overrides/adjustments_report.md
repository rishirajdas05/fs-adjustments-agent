# Manual adjustments review: 2024-Q4

Run `9c17022d161a6bfc` | functional currency USD | materiality threshold 340,325.00 (5% of pre-tax income 6,806,500.00) | explanations: template

## Summary

| Entry | Description | Amount | Decision |
|---|---|---:|---|
| JE-001 | Accrue Q4 bonus pool | 850,000.00 | Accepted |
| JE-002 | Reclassify marketing spend wrongly booked to T&E | 28,500.00 | Accepted With Warnings |
| JE-003 | FX revaluation of EUR cash balance to period-end rate | 11,200.00 | Rejected |
| JE-004 | Bad debt provision top-up | 45,000.00 | Accepted |
| JE-005 | Reclass conference travel from old code | 18,500.00 | Accepted With Warnings |
| JE-006 | Depreciation catch-up — Q4 | 215,000.00 | Accepted |
| JE-007 | Deferred tax true-up | 38,000.00 | Accepted |
| JE-008 | Intercompany settlement — UK sub | 320,000.00 | Rejected |
| JE-009 | Accrue legal fees for ongoing litigation | 75,000.00 | Accepted |
| JE-010 | Reclass long-term debt current portion | 200,000.00 | Accepted |

Accepted: 6 | Accepted With Warnings: 2 | Quarantined: 0 | Rejected: 2

## Entry by entry

**JE-001: Accrue Q4 bonus pool**

Accepted. Will post as submitted.

Before sign-off, note:
  - At 850,000.00 it is above the materiality threshold of 340,325.00 (5% of pre-tax income 6,806,500.00), so a reviewer must sign it off before the statements are released.

Human decision: sign_off by controller@example.test (DEMO: bonus pool agreed to the HR workbook v3). Result: ACCEPTED (warnings signed off).

**JE-002: Reclassify marketing spend wrongly booked to T&E**

Accepted, pending reviewer sign-off. Will post, but someone needs to acknowledge the points below.

Before sign-off, note:
  - Account 6310 has an open data-quality issue in the trial balance (TB_DUPLICATE_ACCOUNT_ROW). Check the entry still makes sense once that is resolved.

Options (none are applied automatically; each has been re-checked against the same rules):
  - JE-002-FIX-A (confidence 50%): Make both sides 28,500.00 (change the credit line). A reclass moves one amount from one account to another, so both sides should be equal. Nothing in the inputs says which figure is right; the source document ('Controller note 2024-12-28') has to settle it.
  - JE-002-FIX-B (confidence 50%): Make both sides 25,000.00 (change the debit line). A reclass moves one amount from one account to another, so both sides should be equal. Nothing in the inputs says which figure is right; the source document ('Controller note 2024-12-28') has to settle it.

Considered and ruled out:
  - Plugging the difference to suspense (9999) was not offered: it would hide the error rather than fix it

Human decision: apply_fix by controller@example.test (DEMO: assume controller note shows 28,500). Result: ACCEPTED_WITH_WARNINGS with JE-002-FIX-A.

**JE-003: FX revaluation of EUR cash balance to period-end rate**

Rejected. Not posted, and there is no safe automatic fix. The preparer needs to resubmit.

What is wrong:
  - The posted revaluation is 11,200.00. Recomputing it from fx_rates.csv for 825,400.00 EUR at the period-end rate 1.095 gives 19,809.60 from the opening rate 1.071 or 10,730.20 from the period average rate 1.082. Neither matches, so the source calculation needs checking.
  - The system already converts EUR balances at the period-end rate when it builds the statements, so this entry would book the same gain a second time.

Options (none are applied automatically; each has been re-checked against the same rules):
  - JE-003-FIX-A (confidence 60%): Withdraw the entry and let the pipeline remeasure the balance. The translation step already values the foreign balance at the period-end rate. Posting this as well would book the gain twice. Right only if the TB carries this balance in transaction currency (see clarifying question 1).
  - JE-003-FIX-B (confidence 25%): Restate to 10,730.20 (period average to period-end basis). 825,400.00 EUR x (1.095 - 1.082). Right only if the ledger carries this balance at the period average rate.
  - JE-003-FIX-C (confidence 15%): Restate to 19,809.60 (opening to period-end basis). 825,400.00 EUR x (1.095 - 1.071). Right only if the ledger carries this balance at the opening rate.

Next step: the preparer should confirm what the entry was meant to do and submit a new one.

Human decision: apply_fix by controller@example.test (DEMO: pipeline remeasures EUR cash, manual reval withdrawn). Result: REJECTED (withdrawn by approver, pipeline handles it).

**JE-004: Bad debt provision top-up**

Accepted. Will post as submitted.

All checks passed: balanced, valid accounts, in period, below materiality.

**JE-005: Reclass conference travel from old code**

Accepted, pending reviewer sign-off. Will post, but someone needs to acknowledge the points below.

Before sign-off, note:
  - Account 6310 has an open data-quality issue in the trial balance (TB_DUPLICATE_ACCOUNT_ROW). Check the entry still makes sense once that is resolved.

Options (none are applied automatically; each has been re-checked against the same rules):
  - JE-005-FIX-A (confidence 40%): Keep 6315 and add it to the COA as 'Conference Travel' under 6000 Operating Expenses. The description ('Reclass conference travel from old code') suggests 6315 is a deliberate new sub-account. That is a COA change and needs the COA owner's approval.

Considered and ruled out:
  - Mapping 6315 to 6310 Travel and Entertainment (score 0.533) was discarded on re-validation: JE_NO_NET_EFFECT: Every account in the entry is debited and credited by the same amount, so it changes nothing

Human decision: apply_fix by coa-owner@example.test (DEMO: new sub-account approved). Result: ACCEPTED_WITH_WARNINGS with JE-005-FIX-A.

**JE-006: Depreciation catch-up — Q4**

Accepted. Will post as submitted.

All checks passed: balanced, valid accounts, in period, below materiality.

**JE-007: Deferred tax true-up**

Accepted. Will post as submitted.

All checks passed: balanced, valid accounts, in period, below materiality.

**JE-008: Intercompany settlement — UK sub**

Rejected. Not posted, and there is no safe automatic fix. The preparer needs to resubmit.

What is wrong:
  - It debits and credits the same account (2170) by 320,000.00. The two lines cancel, so posting it would change no balance while adding a 320,000.00 entry to the audit trail that looks like a real settlement. It may have been meant as: A cash settlement: debit 2170, credit 1110 Cash for 320,000.00; or The UK subsidiary's side of the settlement (its IC receivable), which belongs in the UK entity's books, not this one.

Points for the reviewer:
  - It touches the intercompany account but names no counterparty entity, so it cannot be matched against the other entity's books for elimination.

Next step: the preparer should confirm what the entry was meant to do and submit a new one.

Human decision: accept_as_is by ap-clerk@example.test (DEMO: shows that a hard rule cannot be waived). Result: REFUSED: ['JE_NO_NET_EFFECT'] cannot be waived by an override.

**JE-009: Accrue legal fees for ongoing litigation**

Accepted. Will post as submitted.

All checks passed: balanced, valid accounts, in period, below materiality.

**JE-010: Reclass long-term debt current portion**

Accepted. Will post as submitted.

All checks passed: balanced, valid accounts, in period, below materiality.

## Effect of the posted entries

| Account | Name | Before (USD, Dr+) | Adjustments | After | Entries |
|---|---|---:|---:|---:|---|
| 1121 | Allowance for Doubtful Accounts | -185,000.00 | -45,000.00 | -230,000.00 | JE-004 |
| 1211 | Accumulated Depreciation - PPE | -4,200,000.00 | -215,000.00 | -4,415,000.00 | JE-006 |
| 1250 | Deferred Tax Asset | 320,000.00 | -38,000.00 | 282,000.00 | JE-007 |
| 2120 | Accrued Expenses | -1,150,000.00 | -925,000.00 | -2,075,000.00 | JE-001, JE-009 |
| 2140 | Current Portion of Long-term Debt | -800,000.00 | -200,000.00 | -1,000,000.00 | JE-010 |
| 2210 | Long-term Debt | -6,500,000.00 | 200,000.00 | -6,300,000.00 | JE-010 |
| 6100 | Salaries and Wages | 5,400,000.00 | 850,000.00 | 6,250,000.00 | JE-001 |
| 6300 | Marketing and Advertising | 980,000.00 | 28,500.00 | 1,008,500.00 | JE-002 |
| 6310 | Travel and Entertainment | 283,500.00 | -47,000.00 | 236,500.00 | JE-002, JE-005 |
| 6315 | Conference Travel | 0.00 | 18,500.00 | 18,500.00 | JE-005 |
| 6400 | Professional Fees | 520,000.00 | 75,000.00 | 595,000.00 | JE-009 |
| 6500 | Depreciation Expense | 850,000.00 | 215,000.00 | 1,065,000.00 | JE-006 |
| 6600 | Bad Debt Expense | 95,000.00 | 45,000.00 | 140,000.00 | JE-004 |
| 8200 | Deferred Tax Expense | 120,000.00 | 38,000.00 | 158,000.00 | JE-007 |

## Checks run after posting

- PASS: each posted entry balances
- PASS: batch does not change tb net
- PASS: every account delta equals sum of posted lines
- PASS: every posted line hits a coa account
- PASS: every adjustment traces to a source line
- Abnormal balance flips: none
- TB net (debits minus credits, USD) before: 182,460.20, after: 182,460.20. The adjustments do not change it; the starting imbalance is an upstream problem listed below.

## Upstream data issues this run worked around (not fixed here)

These belong to the TB ingestion and COA mapping stages. They are listed because they affect how far the adjusted numbers can be trusted.

- **COA_CF_CATEGORY_UNSET**: 1150 Other Current Assets has cash-flow category 'TBD' (chart_of_accounts.csv#row 10)
- **COA_CF_CATEGORY_UNSET**: 2170 Intercompany Payable has cash-flow category 'TBD' (chart_of_accounts.csv#row 28)
- **TB_ORPHAN_ACCOUNT**: TB account 9999 'Suspense - Unmapped' is not in the COA (trial_balance.csv#row 63)
- **TB_DUPLICATE_ACCOUNT_ROW**: Account 6310 appears 2 times in USD; could be a double export or a legitimate split. Kept both rows, flagged for review (trial_balance.csv#row 48, trial_balance.csv#row 49)
- **FX_RATE_MISSING**: No period_end rate for GBP; used period_average 1.264 as a flagged fallback (trial_balance.csv#row 4)
- **TB_UNBALANCED**: Pre-adjustment TB does not balance: debits minus credits is 182,460.20 USD after translation (-4,800.00 if currencies are summed as-is)

Informational:

- COA_EMPTY_HEADER: Header 1290 Other Assets has no child accounts
- COA_CF_CATEGORY_UNSET: 3200 Retained Earnings has cash-flow category 'blank'
- COA_POSTABLE_PARENT: 3300 Accumulated OCI is a postable Equity account but also has children (3310); rollups could double count
- COA_CF_CATEGORY_UNSET: 3300 Accumulated OCI has cash-flow category 'blank'
- COA_CF_CATEGORY_UNSET: 3310 FX Translation Reserve has cash-flow category 'blank'
- COA_NO_NORMAL_BALANCE: 7000 Non-Operating Items has no normal balance
- COA_POSTABLE_PARENT: 8000 Income Tax Expense is a postable Expense account but also has children (8100, 8200); rollups could double count
