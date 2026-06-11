# Tax Prep Notes

> **This document is not professional tax advice.** It is a personal reference
> checklist. Given the complexity of your situation — divorce, dependent, multiple
> brokerage accounts, equity compensation — consult a CPA or enrolled agent before
> filing. The notes below flag things worth discussing with them, not telling you
> what to do.

---

## Filing Status

- Filing status has significant impact on tax rates, standard deduction, and credit
  eligibility. For a divorced parent, the two most common options are **Single** or
  **Head of Household** (HOH). HOH is more favorable and applies if you paid more
  than half the cost of keeping a home for a qualifying person (like your son) for
  more than half the year.
- **Who claims the dependent** is typically set in the divorce decree/separation
  agreement. The custodial parent usually has the default right to claim the child,
  but this can be waived with IRS Form 8332. This affects who can claim:
  - Child Tax Credit / Additional Child Tax Credit
  - Dependent Care Credit (Form 2441) — generally requires you to be the custodial
    parent regardless of who claims the dependency exemption
  - Education credits
- Discuss with your tax preparer which parent should claim the dependent each year
  and whether any alternating-year arrangement is in your agreement.

---

## Dependent-Related Credits

### Child Tax Credit (CTC)
- Up to $2,000 per qualifying child under 17, subject to income phase-outs.
- Goes to whoever claims the child as a dependent that year.

### Dependent Care Credit (Form 2441)
- Covers childcare expenses so you (and a spouse, if married) can work.
- Applies to daycare, after-school programs, summer day camps (sleep-away camps
  don't qualify), and similar costs for children under 13.
- Use `fintrack tax tag <txn_id> --category dependent_care` to flag these in fintrack.
- **Summer camp** is a common example — day camp fees qualify; overnight/sleep-away
  camp fees do not.
- The credit is a percentage (typically 20–35%) of up to $3,000 in expenses for
  one child ($6,000 for two or more).

---

## Multiple Brokerage Accounts

You have accounts at Schwab, Stash, and possibly E*Trade. Things to watch:

### 1099-B — Cost Basis Reconciliation
- Each broker sends a 1099-B listing sale proceeds and (usually) cost basis.
- Cost basis method matters: FIFO, specific lot, average cost — check what each
  broker defaults to and whether you want to override it.
- If shares were transferred between brokers, the receiving broker may not have
  original cost basis. You may need to supply it manually on your return (Form 8949).

### Wash Sale Rule
- If you sell a position at a loss and repurchase "substantially identical" securities
  within 30 days before or after the sale, the loss is deferred. Brokers should flag
  this on the 1099-B, but if you trade the same ticker across multiple accounts
  (e.g., sell at a loss in Schwab, rebuy in Stash), only you will know.

### ESPP / RSU Sales
- RSU vest events create ordinary income (W-2); the FMV at vest is your cost basis
  for the shares. The 1099-B reports the sale proceeds — if you subtract FMV at vest,
  the remaining gain/loss is capital.
- ESPP shares: the discount is ordinary income; gains above that are capital. Your
  E*Trade / Schwab 1099-B should have a supplemental statement with corrected cost
  basis. Use it — the uncorrected 1099-B will overstate your gain.
- `fintrack assets equity record` and `equity scan` can help you keep track of
  vest and sale events.

---

## Other Items to Review

### Large One-Time Purchases
- Major purchases (vehicles, appliances, home improvements) generally aren't tax
  deductible for personal use, but they can affect:
  - Sales tax deduction (if you itemize, you can deduct state/local sales tax instead
    of income tax — useful in a year with large purchases)
  - Home improvement basis (if you own a home, keep records for when you sell)
- Use `fintrack flag <txn_id> --type one-time` to mark large one-time items and
  `fintrack tax tag <txn_id> --category other` if you want to flag them for review.

### Medical Expenses
- Deductible only if you itemize, and only the portion exceeding 7.5% of AGI.
- Still worth tracking: counts toward an FSA/HSA reconciliation, and enough
  medical spend can push you over the threshold.
- Use `fintrack tax tag <txn_id> --category medical --note "..."`.

### Charitable Donations
- Cash donations to qualifying 501(c)(3) organizations are deductible if you itemize.
- Non-cash donations (clothing, household goods) require a receipt from the charity
  and, for items over $500, IRS Form 8283.
- Use `fintrack tax tag <txn_id> --category charitable`.

### Mortgage Interest (if applicable)
- If you have a mortgage, Form 1098 from your lender shows deductible interest.
- `fintrack tax docs init` will add a 1098 tracker for any mortgage-type accounts
  linked via Plaid. Add your servicer manually if it isn't linked.

### Student Loan Interest
- Up to $2,500 of student loan interest may be deductible (income phase-outs apply),
  without needing to itemize. Your lender will send Form 1098-E.

---

## Standard vs. Itemized Deductions

For 2025, the standard deduction is approximately:
- Single: $15,000
- Head of Household: $22,500
- Married Filing Jointly: $30,000

Only itemize if your deductible expenses (SALT up to $10,000, mortgage interest,
charitable donations, excess medical) exceed the standard deduction. For most
W-2 earners with moderate mortgage interest and some charitable giving, the
standard deduction often wins — but run the numbers.

---

## Useful fintrack Commands for Tax Season

```powershell
# See all tax-tagged transactions for the year
fintrack tax tag list --year 2025

# Full summary by category
fintrack report tax-summary --year 2025

# Summary with individual transaction drill-down
fintrack report tax-summary --year 2025 --detail

# Check which documents you're still waiting on
fintrack tax docs list --year 2025

# Mark a document received
fintrack tax docs mark <id> --received

# Look up stored reference info
fintrack tax info list

# Drill into a spending category (e.g., to find all medical transactions)
fintrack drill HEALTH --months 12
```

---

## Document Checklist (by institution)

Run `fintrack tax docs init --year <year>` to auto-populate this from your linked
Plaid accounts, then mark each received as it arrives.

Common expected documents:
| Institution       | Document     | Notes |
|---|---|---|
| Employer          | W-2          | Add manually: `fintrack tax docs add --year 2025 --institution "Employer" --doc-type W-2` |
| Bank of America   | 1099-INT     | Only issued if interest ≥ $10 |
| PNC               | 1099-INT     | Only issued if interest ≥ $10 |
| Charles Schwab    | 1099-DIV, 1099-B, 1099-INT | Usually consolidated into one mailing |
| Stash             | 1099-DIV, 1099-B, 1099-INT | Check app/email |
| E*Trade           | 1099-DIV, 1099-B, 1099-INT | Includes ESPP supplemental statement |
| Mortgage servicer | 1098         | Mortgage interest; add manually if servicer isn't in Plaid |

---

*Last updated: 2026-06-11*
