# fintrack

Personal financial management tool. Pulls transactions from Plaid (Bank of
America, Stash, or any supported institution), classifies spending with a
layered pipeline, and pushes analysis to Google Sheets. Includes net cashflow
tracking, recurring expense detection, ntfy.sh push alerts, and optional
Prophet-based spending forecasts.

---

## Features

- **Plaid `/transactions/sync`** with per-page cursor persistence
- **Layered classifier pipeline**: regex rules -> Plaid categories -> LLM (slot ready)
- **Manual override system**: fix misclassified transactions via CLI or Google Sheets
- **Net cashflow view**: income vs expenses with internal transfer detection
- **Google Sheets push**: Summary, Trends (cross-section heatmap), Cashflow, Transactions tabs
- **Recurring expense detection**: auto-detect + manual list, with exclude list for false positives
- **ntfy.sh push alerts**: large transactions, spending spikes, upcoming/missing charges, auth errors
- **Prophet forecasts**: monthly category-level predictions with uncertainty intervals (optional dep)
- **Token encryption**: Fernet-encrypted access_tokens at rest
- **SQLite storage** with `raw_json` safety net

---

## Prerequisites

- Python 3.11+
- A [Plaid developer account](https://dashboard.plaid.com/signup) (free Trial tier for production)
- `pip` or `uv`

For Google Sheets: a Google Cloud project with a service account and the Sheets API enabled.
For push alerts: the [ntfy app](https://ntfy.sh) on your phone (free, no account needed).
For forecasting: `pip install "fintrack[forecast]"` (requires a C++ compiler for Stan).

---

## Installation

```bash
cd fintrack
pip install -e ".[dev]"
```

For Prophet forecasting support:

```bash
pip install -e ".[dev,forecast]"
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
PLAID_SECRET=your_sandbox_secret
PLAID_ENV=sandbox
```

### 2. (Recommended) Enable token encryption

```bash
fintrack keygen
```

Copy the printed `FERNET_KEY=...` line into your `.env`. This encrypts the
Plaid access tokens stored in SQLite. Losing the key means re-linking all
institutions, so keep it safe.

### 3. Link an institution

```bash
fintrack link
```

Opens `http://localhost:5000` -- connect your bank through the Plaid Link
widget. In sandbox mode use username `user_good` / password `pass_good`.
The server exits automatically after a successful link.

Repeat for each institution (Bank of America, Stash, etc.).

### 4. Sync transactions

```bash
fintrack sync
```

Fetches all transactions via `/transactions/sync`. Subsequent syncs are
incremental -- only new, modified, or removed transactions are transferred.
Manual category overrides are preserved across syncs.

### 5. View reports

```bash
fintrack report               # current month spending by category + merchants
fintrack cashflow             # income vs expenses, net position, 6-month trend
fintrack report --month 2026-03
fintrack cashflow --month 2026-03 --trend 12
```

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
| `fintrack report` | Monthly spending by category + top merchants |
| `fintrack report --month 2026-03 --top 20` | Specific month, more merchants |
| `fintrack cashflow` | Net cashflow: income minus expenses |
| `fintrack cashflow --month 2026-03 --trend 12` | Specific month, 12-month trend |

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
GOOGLE_SPREADSHEET_ID=1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms
```

Tabs created/updated by `fintrack push`:

| Tab | Contents |
|---|---|
| **Summary** | Category breakdown + top 15 merchants for the selected month |
| **Trends** | Category x month heatmap (white-to-orange gradient) for last N months |
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
2. Subscribe to any topic name you choose (e.g., `fintrack-leon-abc123`).
   Make it reasonably unique so others don't accidentally subscribe.
3. Add to `.env`:

```env
NTFY_TOPIC=fintrack-leon-abc123
```

Run `fintrack check` to verify alerts are delivered. No account or API key needed.

---

## Recurring Expenses

### Auto-detection

fintrack scans the last `RECURRING_LOOKBACK_DAYS` (default 180) of transactions
and flags merchants that appear at 25-40 day intervals with stable amounts
(coefficient of variation below `RECURRING_AMOUNT_TOLERANCE`, default 20%).
A confidence score (0-1) reflects gap consistency and occurrence count.

### Manual list

Add known recurring charges to `.env` so they are tracked even before enough
history accumulates for auto-detection:

```env
RECURRING_EXPENSES=Netflix|15.99|15,Spotify|9.99|20,Rent|2500|1,AWS|12.50|5
```

Format: `Merchant Name|monthly_amount|day_of_month` (comma-separated).
Manual entries override auto-detected entries for the same merchant.

### Exclude list

Suppress false positives from auto-detection:

```env
RECURRING_EXCLUDE_MERCHANTS=Employer Payroll,Zelle From Dad,Venmo Transfer
```

---

## Cashflow Calculation

By default, any transaction with category `TRANSFER_IN` or `TRANSFER_OUT` is
excluded from income and expense totals (configurable via `CASHFLOW_TRANSFER_CATEGORIES`).

In addition, fintrack auto-detects internal account transfers: same-day debit/credit
pairs across different accounts whose amounts cancel within 2% are flagged as
internal and excluded from cashflow. This prevents a BofA -> Stash transfer from
counting as both an expense and income.

---

## Classifier Pipeline

The chain is configured via `CLASSIFIER_CHAIN` (default: `rules,plaid`).
Each classifier is tried left to right; the first non-None result wins.

```
rules    -- fast regex over merchant_name / name (no external deps)
plaid    -- Plaid's personal_finance_category (always available)
override -- manual corrections always win (applied automatically)
llm      -- drop-in slot (see below)
```

Classifier sources are recorded in the `category_source` column so you can
always see how a transaction was classified.

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
        # Call Anthropic / OpenAI here, return None to pass to next classifier
        ...
```

2. Add to `.env`: `CLASSIFIER_CHAIN=llm,rules,plaid`

No other changes needed -- `build_chain()` picks it up automatically.

---

## Forecasting with Prophet

Install the optional dependency group:

```bash
pip install "fintrack[forecast]"
```

Prophet requires `cmdstanpy` which compiles C++ code. If installation fails,
see the [official docs](https://facebook.github.io/prophet/docs/installation.html).
All other fintrack features work without Prophet.

Prophet is used for:
- **Monthly category-level forecasts** using yearly Fourier seasonality
- **Anomaly detection**: months where actual spend exceeds the 80% prediction interval

Without Prophet, `fintrack forecast` and `fintrack push --forecast` raise a
clear error with install instructions. All other commands are unaffected.

---

## Running Tests

```bash
# Unit + mock tests (no credentials needed)
pytest

# With coverage
pytest --cov=fintrack

# Sandbox integration tests (requires real Plaid sandbox keys in .env)
pytest -m sandbox
```

---

## Database Schema

```sql
items (
    item_id PK, access_token (encrypted), institution_name,
    cursor, last_synced, error_state
)
accounts (
    account_id PK, item_id FK, name, type, subtype
)
transactions (
    transaction_id PK, account_id FK, date, amount,
    merchant_name, raw_name,
    category_primary, category_detailed, category_confidence, category_source,
    pending, raw_json
)
transaction_overrides (
    transaction_id PK, category, subcategory, note,
    overridden_at, override_source
)
recurring_excludes (
    merchant_pattern PK, added_at
)
alert_log (
    id PK, alert_type, message, sent_at, delivered
)
```

`raw_json` stores the full Plaid transaction object so schema changes never lose data.
Migrations are additive-only and run automatically on startup.

---

## Going to Production

1. Sign up for Plaid's Trial plan (free, up to 10 institutions).
2. Set `PLAID_ENV=production` and use your production `PLAID_SECRET`.
3. Set a strong random `FLASK_SECRET_KEY`.
4. Generate a `FERNET_KEY` with `fintrack keygen` and store it safely.
5. Run `fintrack link` once per real institution (BofA will use OAuth).
6. Set up a daily cron job or Windows Scheduled Task:

```
# cron example (daily at 7am)
0 7 * * * cd /path/to/fintrack && fintrack sync && fintrack check && fintrack push
```

> **Never commit `.env` or `service_account.json`** -- they contain secret keys.
> Both are already in `.gitignore`.
