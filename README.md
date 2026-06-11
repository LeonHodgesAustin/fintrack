# fintrack

Personal financial management tool. Pulls transactions from Plaid (Bank of
America, Stash, PNC, Charles Schwab, or any supported institution), classifies
spending with a layered pipeline, tracks net worth across loans, vehicles, and
equity, and pushes analysis to Google Sheets.

---

## Features

- **Tax-prep layer**: tag transactions by tax category (medical, charitable, dependent care, etc.), track expected/received tax documents (W-2, 1099s), and store static reference info (EIN, prior-year AGI)
- **Plaid `/transactions/sync`** with per-page cursor persistence — never re-fetches from scratch
- **Layered classifier pipeline**: regex rules → Plaid categories → LLM (slot ready)
- **Manual override system**: fix misclassified transactions via CLI or Google Sheets
- **Net cashflow view**: income vs expenses with internal transfer detection
- **Google Sheets push**: Summary, Trends (cross-section heatmap), Cashflow, Transactions tabs
- **Recurring expense detection**: auto-detect + manual list, with exclude list for false positives
- **ntfy.sh push alerts**: large transactions, spending spikes, upcoming/missing charges, auth errors
- **Balance snapshot history**: one row per account per sync stored in `balance_snapshots`; `net_worth_snapshots` view aggregates into hourly time-series data points
- **Net worth tracking**: mortgage/auto loan amortization, vehicle depreciation, RSU vesting, ESPP
- **Prophet forecasts**: monthly category-level predictions with uncertainty intervals (optional dep)
- **Token encryption**: Fernet-encrypted access tokens at rest
- **SQLite storage** with `raw_json` safety net

---

## Prerequisites

- Python 3.11+
- A [Plaid developer account](https://dashboard.plaid.com/signup) (free Trial tier for production)
- `pip` or `uv`

For Google Sheets: a Google Cloud project with a service account and the Sheets API enabled.
For push alerts: the [ntfy app](https://ntfy.sh) on your phone (free, no account needed).
For forecasting: `pip install "fintrack[forecast]"` (requires a C++ compiler for Stan).
For net worth / stock prices: `pip install "fintrack[assets]"` (adds yfinance).

> **PowerShell note**: examples below use bash-style `\` for line continuation.
> In PowerShell use a backtick `` ` `` instead, or run the command on a single line.

---

## Installation

```bash
cd fintrack
pip install -e ".[dev]"
```

With optional features:

```bash
pip install -e ".[dev,assets]"           # + net worth / stock prices
pip install -e ".[dev,assets,forecast]"  # + Prophet forecasting
```

---

## Quickstart

### 1. Configure credentials

```bash
cp .env.example .env
```

Fill in at minimum:

```env
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_production_secret
PLAID_ENV=production
PLAID_CLIENT_NAME=YourAppName   # must match name registered in Plaid dashboard
```

### 2. Configure Plaid Link (production only)

In the [Plaid dashboard](https://dashboard.plaid.com/link/data-transparency-v5), open your
default Link customization and enable **Transaction history** under Data Transparency Messaging.
Without this, the Link widget will refuse to open in production.

### 3. (Recommended) Enable token encryption

```bash
fintrack keygen
```

Copy the printed `FERNET_KEY=...` line into your `.env`. This encrypts Plaid access tokens
stored in SQLite. Losing the key means re-linking all institutions, so back it up.

### 4. Link institutions

```bash
fintrack link
```

Opens `http://localhost:5000`. Connect your bank through the Plaid Link widget. Repeat for
each institution. The server exits automatically after each successful link.

In sandbox mode use username `user_good` / password `pass_good`.

### 5. Sync transactions

```bash
fintrack sync
```

Fetches all transactions via `/transactions/sync`. Subsequent syncs are incremental.
Manual category overrides survive syncs.

### 6. View reports

```bash
fintrack report spending               # current month spending by category + merchants
fintrack report networth               # net worth time-series from balance snapshots
fintrack cashflow                      # income vs expenses, net position, 6-month trend
fintrack report spending --month 2026-03
fintrack cashflow --month 2026-03 --trend 12
```

---

## Supported Institutions

Any institution in Plaid's catalog works with `fintrack link`. Notable ones:

| Institution | Status | Notes |
|---|---|---|
| Bank of America | Full support | OAuth — redirects to bofa.com |
| PNC Bank | Full support | OAuth — redirects to pnc.com |
| Charles Schwab | Full support | OAuth — checking/brokerage cash via `transactions` product |
| Stash | Full support | Connects via custodian |
| E*Trade / Morgan Stanley | Try it | Legacy E*Trade may work; Morgan Stanley proper does not |
| Mid-Island Mortgage | Not supported | Small regional servicer; not in Plaid's catalog |

For mortgage servicers and other unsupported institutions, use
`fintrack assets loan add` to track the balance from your servicer's portal directly.

---

## Importing Historical Data

Plaid provides approximately 90 days of transaction history on first link. To import older
data exported from your bank (CSV, OFX):

1. Import transactions with a consistent ID prefix (e.g. `bofa_`).
2. After syncing Plaid, run the overlap cleanup script to remove any duplicates
   in the period Plaid now covers:

```bash
python scripts/dedupe_imported.py --dry-run   # preview
python scripts/dedupe_imported.py             # delete overlapping imports
```

The script finds the earliest Plaid transaction date and removes all imported records
on or after that date. Plaid is the authoritative source for its coverage window;
the import covers everything older.

---

## CLI Reference

### Core

| Command | Description |
|---|---|
| `fintrack link` | Start link server to connect an institution |
| `fintrack sync` | Sync all linked items (incremental) |
| `fintrack sync --item <id>` | Sync a single item |
| `fintrack reauth --item <id>` | Re-authenticate a broken item |
| `fintrack items list` | Show linked institutions and sync status |
| `fintrack keygen` | Generate a Fernet encryption key |

### Reporting

| Command | Description |
|---|---|
| `fintrack report spending` | Monthly spending by category + top merchants |
| `fintrack report spending --month 2026-03 --top 20` | Specific month, more merchants |
| `fintrack report networth` | Net worth time-series from balance snapshots |
| `fintrack report networth --limit 60` | Show last 60 sync snapshots |
| `fintrack cashflow` | Net cashflow: income minus expenses |
| `fintrack cashflow --month 2026-03 --trend 12` | Specific month, 12-month trend |
| `fintrack networth` | Net worth snapshot across all asset types |
| `fintrack networth --offline` | Use cached stock prices (no internet required) |

### Review and correction

| Command | Description |
|---|---|
| `fintrack review` | Interactively fix low-confidence transactions |
| `fintrack review --limit 100 --confidence 0.75` | Custom thresholds |

`fintrack review` pages through transactions that are `UNCATEGORIZED` or have
confidence below the threshold (default 0.60). For each one you can type a
new category, optionally add a note, or press Enter to skip. Corrections are
written to SQLite immediately and survive future syncs.

### Google Sheets

| Command | Description |
|---|---|
| `fintrack push` | Push all tabs to Google Sheets |
| `fintrack push --month 2026-03` | Specific month for Summary tab |
| `fintrack push --trends 6 --days 60` | 6-month Trends, 60-day Transactions |
| `fintrack push --forecast` | Include Forecast tab (requires prophet) |

`fintrack push` first reads any "Override Category" values you have entered
in the Transactions tab and commits them to the local DB before rewriting
the sheet. This means fixing categories in Sheets is a first-class workflow.

### Alerts

| Command | Description |
|---|---|
| `fintrack check` | Run all alert checks, print results, send via ntfy.sh |
| `fintrack check --no-send` | Check only, do not send push notifications |

Alert types checked:
- Auth errors (items requiring re-authentication)
- Large single transactions (above `LARGE_TRANSACTION_THRESHOLD`)
- Month-over-month spending spikes (above `SPENDING_SPIKE_PCT`)
- Upcoming recurring charges (due within `RECURRING_UPCOMING_DAYS` days)
- Missing expected recurring charges (overdue by `RECURRING_MISSING_GRACE_DAYS`)

### Forecasting (requires prophet)

| Command | Description |
|---|---|
| `fintrack forecast` | 3-month category forecasts + anomalous months |
| `fintrack forecast --ahead 6 --no-anomalies` | 6-month forecast only |

### Assets — loans

| Command | Description |
|---|---|
| `fintrack assets loan add` | Add a mortgage or auto loan |
| `fintrack assets loan list` | List loans with current balances |
| `fintrack assets loan schedule --loan <id>` | Show amortization schedule |

### Assets — vehicles

| Command | Description |
|---|---|
| `fintrack assets vehicle add` | Add a vehicle for depreciation tracking |
| `fintrack assets vehicle list` | List vehicles with estimated current values |

### Assets — equity

| Command | Description |
|---|---|
| `fintrack assets equity add-rsu` | Add an RSU grant |
| `fintrack assets equity add-espp` | Add an ESPP plan |
| `fintrack assets equity list` | Show grants, vested shares, and live values |
| `fintrack assets equity record` | Record a vest event, sale, or ESPP purchase |
| `fintrack assets equity scan` | Scan brokerage transactions for potential stock sales |

---

## Net Worth Tracking

`fintrack networth` (requires `pip install "fintrack[assets]"`) aggregates:

```
Net Worth = liquid_accounts + vehicle_values + vested_equity_value − loan_balances
```

Unvested RSU shares are shown separately and not counted in net worth (they haven't vested yet).

### Adding a loan

`--principal` is the **balance as of `--start`** — not necessarily the original loan amount.
Two equivalent ways to enter an existing loan:

**From original closing documents** (preferred if you have them):
```powershell
fintrack assets loan add --name "Primary Mortgage" --type mortgage --principal 380000 --rate 6.875 --term 360 --start 2023-04-01
```

**From your servicer's current balance** (useful when original docs aren't handy):
```powershell
fintrack assets loan add --name "Mid-Island Mortgage" --type mortgage --principal 208606.27 --rate 2.75 --term 295 --start 2026-07-01
```

Use the current outstanding balance, months remaining to payoff, and next payment date.
The current balance will be correct today and decline on the correct schedule going forward.

### Adding a vehicle

```powershell
fintrack assets vehicle add --name "2022 Honda CR-V" --price 34000 --date 2022-08-15
```

Uses 18%/yr declining-balance depreciation by default. Override with `--rate 15` (percent).
This is an estimate — for a precise figure use KBB or Carfax.

### Adding RSU grants

```powershell
fintrack assets equity add-rsu --ticker CSCO --date 2024-01-15 --shares 1000 --cliff 12 --vest 48
```

Default schedule: 12-month cliff (25% vests), then monthly for 3 more years. Adjust
`--cliff`, `--vest`, and `--freq monthly|quarterly|annual` to match your grant agreement.

### Adding an ESPP plan

```powershell
fintrack assets equity add-espp --ticker CSCO --start 2024-01-01
```

Defaults: 10% contribution, 15% discount, 6-month purchase periods, 24-month lookback.

**Important**: the `--lookback` value determines how far back the "lower of" price
comparison reaches. Standard Cisco ESPP uses 24 months (offering period start vs.
purchase date). Some interpret it as 6 months (purchase period only). The difference
is significant when the stock has moved. Verify against your plan documents or the
E*Trade portal before finalizing.

The purchase price formula is:
```
purchase_price = min(price at lookback_start, price at purchase_date) × (1 − discount)
```

### Recording stock sales

```powershell
# Scan recent brokerage transactions for potential sales
fintrack assets equity scan --days 60

# Record a confirmed sale
fintrack assets equity record --grant 1 --type sell --date 2026-05-15 --shares 100 --price 54.32
```

---

## Google Sheets Setup

1. Create a Google Cloud project and enable the **Google Sheets API**.
2. Create a **Service Account**, download the JSON key as `service_account.json`
   in the project root.
3. Create a new Google Sheet and **share it** with the service account email
   address (Editor access).
4. Copy the spreadsheet ID from the URL and add to `.env`:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
```

Tabs created/updated by `fintrack push`:

| Tab | Contents |
|---|---|
| **Summary** | Category breakdown + top 15 merchants for the selected month |
| **Trends** | Category × month heatmap (white-to-orange gradient) for last N months |
| **Cashflow** | Income / expenses / net for current month + monthly trend |
| **Transactions** | Recent transactions with **Override Category** and **Override Note** columns |
| **Forecast** | Prophet predictions (only with `--forecast` flag) |

### Override Roundtrip

In the Transactions tab, type a Plaid category name into the "Override Category"
column for any misclassified transaction. The next `fintrack push` will pick
up your correction, write it to the local DB, and re-apply it on all future syncs.
The "Override Note" column lets you add a reason (optional).

---

## ntfy.sh Setup

1. Install the [ntfy app](https://ntfy.sh) on your phone (iOS or Android).
2. Subscribe to any topic name you choose (e.g., `fintrack-abc123`).
   Make it reasonably unique so others don't accidentally subscribe.
3. Add to `.env`:

```env
NTFY_TOPIC=fintrack-abc123
```

Run `fintrack check` to verify alerts are delivered. No account or API key needed.

---

## Recurring Expenses

### Auto-detection

fintrack scans the last `RECURRING_LOOKBACK_DAYS` (default 180) of transactions
and flags merchants that appear at 25-40 day intervals with stable amounts
(coefficient of variation below `RECURRING_AMOUNT_TOLERANCE`, default 20%).

### Manual list

Add known recurring charges to `.env` so they are tracked even before enough
history accumulates for auto-detection:

```env
RECURRING_EXPENSES=Netflix|15.99|15,Spotify|9.99|20,Rent|2500|1
```

Format: `Merchant Name|monthly_amount|day_of_month` (comma-separated).
Manual entries override auto-detected entries for the same merchant.

### Exclude list

Suppress false positives from auto-detection:

```env
RECURRING_EXCLUDE_MERCHANTS=Employer Payroll,Zelle From Dad
```

---

## Cashflow Calculation

Transactions in `TRANSFER_IN` or `TRANSFER_OUT` categories are excluded from
income and expense totals by default (configurable via `CASHFLOW_TRANSFER_CATEGORIES`).

fintrack also auto-detects internal account transfers: same-day debit/credit pairs
across different accounts whose amounts cancel within 2% are excluded from cashflow.
This prevents a BofA → Stash transfer from counting as both an expense and income.

---

## Classifier Pipeline

The chain is configured via `CLASSIFIER_CHAIN` (default: `rules,plaid`).
Each classifier is tried left to right; the first non-None result wins.

```
rules    -- fast regex over merchant_name / name (no external deps)
plaid    -- Plaid's personal_finance_category (always available)
override -- manual corrections always win (applied automatically at sync time)
llm      -- drop-in slot (see below)
```

### Adding an LLM classifier

1. Create `fintrack/classification/llm.py`:

```python
from .base import ClassificationResult, TransactionClassifier

class LLMClassifier(TransactionClassifier):
    @property
    def name(self) -> str:
        return "llm"

    def classify(self, transaction: dict) -> ClassificationResult | None:
        # transaction is the full Plaid transaction dict
        # Return None to pass to the next classifier
        ...
```

2. Add to `.env`: `CLASSIFIER_CHAIN=llm,rules,plaid`

No other changes needed — `build_chain()` picks it up automatically.

---

## Forecasting with Prophet

```bash
pip install "fintrack[forecast]"
```

Prophet requires `cmdstanpy` which compiles C++ code on install. If it fails,
see the [official docs](https://facebook.github.io/prophet/docs/installation.html).
All other fintrack features work without it.

---

## Running Tests

```bash
pytest                        # unit + mock tests (no credentials needed)
pytest --cov=fintrack          # with coverage
pytest -m sandbox              # integration tests (requires .env with sandbox keys)
```

---

## Tax Prep

Tag transactions, track documents, and store reference info for year-end filing.
See [docs/tax_notes.md](docs/tax_notes.md) for a general checklist and gotchas.

### Tagging transactions

```powershell
# Tag a transaction with a tax category
fintrack tax tag txn-abc123 --category dependent_care --note "echo hill summer camp"
fintrack tax tag txn-xyz789 --category medical         --note "vision exam + glasses"
fintrack tax tag txn-def456 --category charitable      --note "Red Cross donation"

# Available categories:
#   medical, charitable, dependent_care, education, home_office,
#   business, investment, alimony_paid, state_local_tax, other

# List all tagged transactions (optionally filter by year or category)
fintrack tax tag list --year 2025
fintrack tax tag list --year 2025 --category dependent_care

# Remove a tag
fintrack tax tag rm <tag_id>
```

### Tax summary report

```powershell
# Totals by category for the year
fintrack report tax-summary --year 2025

# With individual transactions listed under each category
fintrack report tax-summary --year 2025 --detail
```

### Document tracker

```powershell
# Auto-populate expected docs from linked Plaid institutions
fintrack tax docs init --year 2025

# Add a document manually (e.g., W-2 from employer)
fintrack tax docs add --year 2025 --institution "Cisco" --doc-type W-2

# List all expected/received documents
fintrack tax docs list --year 2025

# Mark a document received (date defaults to today)
fintrack tax docs mark <id> --received
fintrack tax docs mark <id> --received --date 2026-01-31

# Mark not received (e.g., to correct a mistake)
fintrack tax docs mark <id> --not-received

# Remove a document entry
fintrack tax docs rm <id>
```

Available document types: `W-2`, `1099-INT`, `1099-DIV`, `1099-B`, `1099-NEC`,
`1099-MISC`, `1099-R`, `1098`, `1098-E`, `SSA-1099`, `other`.

### Reference info

```powershell
# Store static reference info for filing season
fintrack tax info set employer_ein 12-3456789
fintrack tax info set prior_year_agi 95000
fintrack tax info set schwab_acct_last4 4321  # last 4 only — never full account numbers

# List all stored info
fintrack tax info list

# Remove an entry
fintrack tax info rm employer_ein
```

> **Security note**: Do NOT store full SSNs, full account numbers, or complete
> sensitive identifiers in `tax info`. Last 4 digits are fine for reference.

---

## Database Schema

### Transaction tables

```sql
items        (item_id PK, access_token (encrypted), institution_name, cursor, last_synced, error_state)
accounts     (account_id PK, item_id FK, name, type, subtype)
transactions (transaction_id PK, account_id FK, date, amount,
              merchant_name, raw_name,
              category_primary, category_detailed, category_confidence, category_source,
              pending, raw_json)
transaction_overrides (transaction_id PK, category, subcategory, note, overridden_at, override_source)
recurring_excludes    (merchant_pattern PK, added_at)
alert_log             (id PK, alert_type, message, sent_at, delivered)
balance_snapshots     (snapshot_id PK, account_id FK, captured_at, current_balance,
                       available_balance, limit_amount, iso_currency_code)
budget_adjustments    (id PK, label, monthly_amount, category, notes, active, created_at)
budget_targets        (category PK, target_amount, notes, updated_at)
tax_tags              (tag_id PK, transaction_id FK, tax_category, note, tax_year, created_at)
tax_documents         (id PK, year, institution, doc_type, received, received_date, notes, created_at)
tax_info              (key PK, value, notes, updated_at)
```

### Views

```sql
-- One row per hour-bucket; aggregates balance_snapshots into net worth time series.
-- Assets = depository/investment/other accounts; liabilities = credit/loan accounts.
net_worth_snapshots  (snapshot_hour, total_assets, total_liabilities, net_worth)
```

`balance_snapshots` is written automatically by `fintrack sync` — one row per account per sync run. Use `fintrack report networth` to display the time series.

### Asset tables

```sql
loans       (id PK, name, loan_type, principal, annual_rate, term_months,
             start_date, monthly_payment, extra_json, created_at)
vehicles    (id PK, name, purchase_price, purchase_date,
             annual_depreciation, extra_json, created_at)
equity_grants (id PK, ticker, grant_type, grant_date,
               total_shares, cliff_months, vest_months, vest_frequency,
               contribution_rate, discount_rate, purchase_period_months,
               lookback_months, extra_json, created_at)
equity_transactions (id PK, grant_id FK, txn_type, txn_date,
                     shares, price_per_share, gross_amount, notes, created_at)
stock_price_cache   (ticker, price_date PK, close_price, fetched_at)
```

`raw_json` stores the full Plaid transaction object so schema changes never lose data.
All migrations run automatically on startup and are additive-only.

---

## Going to Production

1. Sign up for Plaid's Trial plan (free, up to 10 institutions).
2. Set `PLAID_ENV=production` and use your production `PLAID_SECRET`.
3. Configure Data Transparency Messaging in the [Plaid dashboard](https://dashboard.plaid.com/link/data-transparency-v5).
4. Set a strong random `FLASK_SECRET_KEY`.
5. Generate a `FERNET_KEY` with `fintrack keygen` and back it up safely.
6. Run `fintrack link` once per institution (BofA and PNC will use OAuth).
7. Set up a daily scheduled task:

```powershell
# Windows Task Scheduler (run daily at 7am)
# Action: powershell.exe -Command "cd C:\projects\fintrack; fintrack sync; fintrack check; fintrack push"
```

```bash
# cron (Linux/macOS)
0 7 * * * cd /path/to/fintrack && fintrack sync && fintrack check && fintrack push
```

> **Never commit `.env` or `service_account.json`** — they contain secret keys.
> Both are already in `.gitignore`.
