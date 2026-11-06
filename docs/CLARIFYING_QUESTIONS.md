# Clarifying questions (to send before starting)

The brief allows up to three. Each one targets an accounting-semantics gap where guessing would change the numbers, not just the code.

**1. FX basis of the TB.** Are the EUR and GBP rows on `trial_balance.csv` in transaction currency, or already in USD? Are those balances held by the USD-functional entity (so remeasurement goes to P&L) or by a foreign subsidiary (so translation goes to the CTA in 3310)? GBP has no period-end rate. Should we block, or fall back to the period-average rate with a flag?
*Why it matters:* translated at the closing rate, the TB is out by 182,460.20. Summed as-is, it is out by 4,800.00. The answer also decides whether JE-003's manual revaluation is needed or a double count.

**2. Period and equity roll-forward.** Are the P&L balances Q4-only or year-to-date, and is `prior_period_tb.csv` the Q3 close or the FY2023 close? Retained earnings moves from 5,180,000 to 7,240,000 with nothing to explain it. Was it dividends, a restatement or an interim close? And is the prior TB (balance sheet only, out of balance by 2,832,800) a partial extract?
*Why it matters:* this decides the SOCIE opening balance, the comparatives, and whether 6905 to 6900 is a rename to map or a real change.

**3. Adjustment policy and intent.** For invalid entries, should the system reject them outright, or propose a fix that a reviewer approves? What was JE-008 meant to do (settle against cash, or record the UK subsidiary's receivable)? Is there a materiality threshold for reviewer sign-off?
*Why it matters:* this decides the accept, quarantine and reject boundary, which is the core of the prototype slice.

## What the prototype assumes until these are answered
1. Transaction currency. The pipeline remeasures EUR cash at the period-end rate, so JE-003 is quarantined as a likely double count. GBP uses the average rate as a flagged fallback.
2. Not needed by the adjustments slice. Raised as upstream issues in the report.
3. Quarantine with proposed fixes. JE-008 is rejected because no safe fix exists. Materiality is 5% of pre-tax income (configurable in `config/policy.json`).
