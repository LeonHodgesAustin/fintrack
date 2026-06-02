# Financial Institutions

Track all accounts here -- linked via Plaid or not. Update the Plaid status
column as you link new institutions with `fintrack link`.

---

## Banks

| Institution | Account | Type | Plaid Linked | Notes |
|---|---|---|---|---|
| Bank of America | Checking (...xxxx) | Checking | YES | Primary checking |
| Bank of America | Savings (...xxxx) | Savings | YES | Emergency fund |
| | | | | |

## Investment / Brokerage

| Institution | Account | Type | Plaid Linked | Notes |
|---|---|---|---|---|
| Stash | Investment | Brokerage / Banking | YES | Micro-investing + debit |
| | | | | |
| | | | | |

## Retirement

| Institution | Account | Type | Plaid Linked | Notes |
|---|---|---|---|---|
| | 401(k) | Employer 401(k) | NO | Check if employer plan is on Plaid |
| | IRA | Traditional/Roth IRA | | |
| | | | | |

## Credit Cards

| Institution | Card | Limit | Plaid Linked | Notes |
|---|---|---|---|---|
| | | | | |
| | | | | |

## Loans

| Institution | Type | Balance | Plaid Linked | Notes |
|---|---|---|---|---|
| | | | | |

## Other (PayPal, Venmo, HSA, etc.)

| Institution | Type | Plaid Linked | Notes |
|---|---|---|---|
| | | | |

---

## Plaid Linking Status

Run `fintrack items list` to see what is currently linked and when it was last synced.

Institutions confirmed on Plaid's Trial plan with OAuth (no extra approval needed):
- Bank of America
- Chase
- Wells Fargo
- Capital One
- Citi
- US Bank
- PNC
- American Express

For institutions not on this list, check https://plaid.com/docs/institutions/

---

## Notes

- Stash banking is issued through Stride Bank. Appears in Plaid as "Stash".
- For employer 401(k) plans: check if your plan provider (Fidelity, Vanguard,
  Empower, etc.) is supported. Most major providers are on Plaid.
- HSA accounts vary -- some providers support Plaid, others require CSV export.
- Update the "Plaid Linked" column to YES/NO/PARTIAL as you link institutions.
