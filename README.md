# fintrack

Personal financial management tool. Pulls transactions from Plaid (Bank of America, Stash, and any other supported institution) and characterizes spending using a layered classifier pipeline.

## Features

- **Plaid `/transactions/sync`** with cursor persistence — never re-fetches from scratch
- **Classifier chain** — rules → Plaid category → LLM (drop-in slot ready)
- **SQLite storage** with `raw_json` so schema changes never lose data
- **Rich CLI** — link, sync, report, reauth in one command
- **Sandbox-first** — full workflow testable without real bank credentials

---

## Prerequisites

- Python 3.11+
- A [Plaid developer account](https://dashboard.plaid.com/signup) (free sandbox tier)
- `pip` or `uv`

---

## Installation

```bash
cd fintrack
pip install -e ".[dev]"
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install -e ".[dev]"
```

---

## Sandbox Quickstart

### 1. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in your Plaid sandbox keys from the [dashboard](https://dashboard.plaid.com/developers/keys):

```env
PLAID_CLIENT_ID=your_client_id
PLAID_SECRET=your_sandbox_secret
PLAID_ENV=sandbox
```

Leave everything else at defaults for now.

### 2. Link a sandbox institution

```bash
fintrack link
```

This starts a local Flask server on `http://localhost:5000`. Open that URL in your browser. You'll see the Plaid Link widget — use Plaid's sandbox credentials to connect a fake institution:

- **Username:** `user_good`
- **Password:** `pass_good`
- Pick any institution (e.g., "First Platypus Bank")

After a successful link the terminal shows the item is saved to `fintrack.db`.

### 3. Sync transactions

```bash
fintrack sync
```

Pulls all historical sandbox transactions (Plaid generates ~90 days of fake data). The cursor is saved to SQLite so subsequent syncs are incremental.

```
Syncing First Platypus Bank (abc12345…)
  +347 added  ~0 modified  -0 removed
```

### 4. View a report

```bash
fintrack report
```

Shows spending by category, top merchants, and a 6-month MoM trend for the current month. Pass `--month 2024-03` to report on a specific month.

---

## Linking Bank of America and Stash

Repeat `fintrack link` for each institution. Each run stores a separate Item in the DB with its own cursor. `fintrack sync` processes all linked items in sequence.

For **Stash**: select "Stash" in the institution search. Stash exposes investment account transactions via Plaid.

---

## CLI Reference

| Command | Description |
|---|---|
| `fintrack link` | Start link server to connect an institution |
| `fintrack sync` | Sync all linked items |
| `fintrack sync --item <item_id>` | Sync a single item |
| `fintrack report` | Current month summary |
| `fintrack report --month 2024-03` | Specific month |
| `fintrack report --top 20` | Show 20 top merchants |
| `fintrack reauth --item <item_id>` | Fix a broken/expired item |
| `fintrack items list` | Show all linked institutions |

---

## Configuration

All config is read from `.env` (or environment variables). See `.env.example` for full documentation.

| Variable | Default | Description |
|---|---|---|
| `PLAID_CLIENT_ID` | — | Required |
| `PLAID_SECRET` | — | Required |
| `PLAID_ENV` | `sandbox` | `sandbox` / `development` / `production` |
| `DB_PATH` | `fintrack.db` | SQLite file path |
| `CLASSIFIER_CHAIN` | `rules,plaid` | Ordered chain of classifier names |
| `LINK_SERVER_PORT` | `5000` | Port for the link web server |
| `FLASK_SECRET_KEY` | `dev-secret-change-me` | Change before any real use |

---

## Classifier Pipeline

The chain is configured via `CLASSIFIER_CHAIN` and resolved left-to-right. Each classifier either returns a result or passes to the next.

```
rules  → fast regex over merchant name (no external deps)
plaid  → Plaid's personal_finance_category (always available, lower precision)
llm    → (slot ready, not yet implemented)
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
        # Call Anthropic / OpenAI here
        ...
```

2. Set `CLASSIFIER_CHAIN=llm,rules,plaid` in `.env`
3. `build_chain()` picks it up automatically — no other changes needed

---

## Running Tests

```bash
# Unit + mock tests (no credentials needed)
pytest

# With coverage
pytest --cov=fintrack

# Sandbox integration tests (requires .env with real sandbox keys)
pytest -m sandbox
```

---

## Database Schema

```sql
items        (item_id PK, access_token, institution_name, cursor, last_synced)
accounts     (account_id PK, item_id FK, name, type, subtype)
transactions (transaction_id PK, account_id FK, date, amount,
              merchant_name, raw_name,
              category_primary, category_detailed, category_confidence, category_source,
              pending, raw_json)
```

`raw_json` stores the full Plaid transaction object so schema changes never lose data.

---

## Going to Production

1. Flip `PLAID_ENV=production` and provide your production `PLAID_SECRET`
2. Set a strong random `FLASK_SECRET_KEY`
3. Run `fintrack link` once per real institution
4. Set up a cron job or systemd timer for `fintrack sync`

> **Never commit `.env`** — it contains your Plaid secret keys.
