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

## HSA / FSA

- **HSA (Health Savings Account)**: Contributions are pre-tax (or deductible if made
  outside payroll). Distributions for qualified medical expenses are tax-free. The
  custodian will send **Form 1099-SA** for distributions; contributions via payroll
  show up on your W-2.
  - Use `fintrack tax tag <txn_id> --category hsa_fsa` for medical expenses paid
    from your HSA/FSA so you can reconcile them.
- **FSA**: Use-it-or-lose-it employer accounts. Contributions are pre-tax via payroll
  (on your W-2). Usually no 1099 issued unless there's a distribution event.
- Keep receipts for all HSA/FSA-reimbursed expenses — the IRS can ask.

---

## Self-Employed / 1099 / Side Work (Schedule C)

If you have freelance income, consulting, or side work reported on a 1099-NEC, you file
Schedule C. Key deductions:

- **Home office** (Form 8829 or simplified method): Only for a space used *regularly
  and exclusively* for business. W-2 employees generally cannot claim this since
  TCJA 2017 eliminated the employee home office deduction — it applies to self-employed.
- **Equipment, software, subscriptions**: Direct business costs are deductible.
- **Phone and internet**: If used partly for business, you can deduct the business
  portion (requires good records of usage split).
- **Vehicle mileage**: Business miles at the IRS standard mileage rate (67¢/mile for
  2024; verify for the current year). Keep a mileage log.
- **Health insurance premiums**: Self-employed health insurance deduction (above-the-line,
  not just Schedule C).
- Use `fintrack tax tag <txn_id> --category self_employed` for all 1099-related
  business expenses. Tag home office costs separately with `--category home_office`.

> **Reminder**: self-employment tax (SE tax) is roughly 15.3% on net self-employment
> income. The deduction for half of SE tax offsets this somewhat.

---

## Energy-Efficient Home Improvement Credits

Two main credits under the Inflation Reduction Act (extended through at least 2032):

### Energy Efficient Home Improvement Credit (Form 5695, §25C)
- Up to **30% of cost**, capped at $1,200/year total (with some sub-limits per category).
- Qualifying items: heat pumps (up to $2,000 cap separately), insulation/air sealing,
  exterior windows/skylights, exterior doors, energy audits, central A/C, water heaters.
- Requires the item to meet specific efficiency standards (ask the installer for the
  Manufacturer Certification Statement).

### Residential Clean Energy Credit (Form 5695, §25D)
- **30% of cost** for solar panels, solar water heaters, battery storage, geothermal
  heat pumps, fuel cells.
- No dollar cap; carries forward if it exceeds your tax liability.

### EV Tax Credit (§30D / §25E)
- New EVs: up to $7,500 (subject to vehicle MSRP and buyer income limits).
- Used EVs: up to $4,000 or 30% of price, whichever is less.
- EV charger installation: up to **30% of cost** (§30C), capped at $1,000 for residential.

Use `fintrack tax tag <txn_id> --category energy_credit` for any of these purchases.

---

## Mortgage Interest and Property Taxes

### Mortgage Interest (Form 1098)
- Deductible if you itemize. Your lender sends Form 1098 showing interest paid.
- `fintrack tax docs init` will add a 1098 tracker for mortgage accounts linked via
  Plaid; add your servicer manually if it isn't linked.
- Use `fintrack tax tag <txn_id> --category mortgage_interest` if you want to track
  individual payment transactions.

### Property Taxes
- Deductible as part of SALT (state and local taxes), capped at $10,000 combined with
  state income/sales tax.
- Use `fintrack tax tag <txn_id> --category state_local_tax` for property tax payments.

---

## Estimated Tax Payments

- If you have self-employment income, investment gains, or other non-W-2 income, you
  may owe quarterly estimated taxes (IRS Form 1040-ES), due April/June/September/January.
- Payments made via IRS Direct Pay or state portals show up as debits — tag them:
  `fintrack tax tag <txn_id> --category estimated_tax`
- These reduce your balance due (or increase your refund) at filing time.
- The IRS safe-harbor rule: you avoid underpayment penalties if you pay at least 90%
  of current-year tax OR 100% of prior-year tax (110% if prior-year AGI > $150,000).

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

## Education Expenses

### American Opportunity Credit (AOTC)
- Up to $2,500/year per eligible student in the first four years of higher education.
- 40% refundable. Requires Form 1098-T from the institution.

### Lifetime Learning Credit (LLC)
- Up to $2,000/year (20% of up to $10,000 in expenses). Not refundable.
- Available for any year of education, including graduate courses and professional
  development.

### 529 Withdrawals
- Federal income-tax-free when used for qualifying education expenses. Some states
  also offer a deduction on contributions.
- Use `fintrack tax tag <txn_id> --category education` to track tuition/fees/books.

---

## Items NOT Worth Tracking (Common Misconceptions)

### Job Search / Moving Expenses
- **Job search costs** (resume prep, recruiters, travel for interviews) are **not**
  federally deductible for employees under TCJA (2017 through at least 2025). Don't
  waste time tracking them.
- **Moving expenses** for employees are similarly **not** deductible federally under
  TCJA. The exception is active-duty military moves; everyone else doesn't get it.

### Personal Legal Fees
- Generally not deductible. Attorney fees related to a divorce are personal expenses
  (not deductible), though fees for tax advice within the divorce context may be
  partially deductible — ask your attorney to itemize the bill.

### Unreimbursed Employee Expenses
- W-2 employees cannot deduct unreimbursed job expenses (tools, uniforms, home office)
  federally under TCJA. If your employer doesn't reimburse via an accountable plan,
  those costs are simply out-of-pocket.

---

## Other Items to Review

### Large One-Time Purchases
- Major purchases (vehicles, appliances, home improvements) generally aren't tax
  deductible for personal use, but they can affect:
  - Sales tax deduction (if you itemize, you can deduct state/local sales tax instead
    of income tax — useful in a year with large purchases)
  - Home improvement basis (if you own a home, keep records for when you sell)
- Use `fintrack flag <txn_id> --type one-time` to mark large one-time items and
  `fintrack tax tag <txn_id> --category other` if you want to flag them for tax review.

### Medical Expenses
- Deductible only if you itemize, and only the portion exceeding 7.5% of AGI.
- Still worth tracking: counts toward HSA reconciliation, and enough spend can push
  you over the threshold.
- Use `fintrack tax tag <txn_id> --category medical --note "..."`.

### Charitable Donations
- Cash donations to qualifying 501(c)(3) organizations are deductible if you itemize.
- Non-cash donations (clothing, household goods) require a receipt and, for items
  over $500, IRS Form 8283.
- Use `fintrack tax tag <txn_id> --category charitable`.

### Student Loan Interest
- Up to $2,500 of student loan interest may be deductible (income phase-outs apply),
  without needing to itemize. Your lender sends Form 1098-E.

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

## Local Document Archive

Keep actual PDF documents (W-2s, 1099s, prior-year returns) in a local folder
**outside this repo** (or add `tax_documents/` to your `.gitignore`). Suggested layout:

```
tax_documents/
  2024/
    W2_Employer_2024.pdf
    1099-B_Schwab_2024.pdf
    1099-INT_BofA_2024.pdf
  2025/
    1099-INT_BofA_2025.pdf
    1099-B_Schwab_consolidated_2025.pdf
    W-2_Cisco_2025.pdf
```

Use `fintrack tax docs scan` to link files to expected entries:

```powershell
fintrack tax docs scan ./tax_documents --year 2025
fintrack tax docs scan ./tax_documents/2025    # year auto-detected from path

# Preview without writing to DB
fintrack tax docs scan ./tax_documents --year 2025 --dry-run
```

The scanner matches on keywords in filenames (W-2/W2, 1099-INT, 1099-B, 1098, etc.)
and records the file path in the database. `fintrack tax docs list` will then show
the linked filename alongside each expected document.

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

# Scan local folder for document files
fintrack tax docs scan ./tax_documents --year 2025
fintrack tax docs scan ./tax_documents/2025

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
| HSA custodian     | 1099-SA      | HSA distributions; add if you have an HSA |
| University / school | 1098-T     | Tuition; add if claiming education credits |

---

*Last updated: 2026-06-11*
