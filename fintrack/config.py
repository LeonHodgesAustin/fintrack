from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    plaid_client_id: str
    plaid_secret: str
    plaid_env: Literal["sandbox", "production"] = "sandbox"

    db_path: str = "fintrack.db"

    # Plaid products to request during Link. "transactions" covers all depository
    # and credit accounts. Add "investments" only if you want to pull holdings/trades
    # from a brokerage (requires implementing investment sync endpoints separately).
    plaid_products: str = "transactions"

    # Comma-separated classifier names; resolved by classification.build_chain()
    classifier_chain: str = "rules,plaid"

    plaid_client_name: str = "MyFintrackExtreme"
    link_server_port: int = 5000
    flask_secret_key: str = "dev-secret-change-me"
    link_customization_name: str = "default"

    # Token encryption -- generate with `fintrack keygen`, store in .env.
    fernet_key: str = ""

    # Google Sheets
    google_service_account_file: str = "service_account.json"
    google_spreadsheet_id: str = ""

    # ntfy.sh alerts -- set NTFY_TOPIC to enable push notifications.
    # Just pick any unique topic name; subscribe in the ntfy app on your phone.
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Alert thresholds -- all configurable
    large_transaction_threshold: float = 500.0
    large_transaction_lookback_days: int = 1
    spending_spike_pct: float = 50.0
    forecast_anomaly_sigma: float = 2.0

    # Recurring expense detection thresholds
    recurring_window_days: int = 5
    recurring_amount_tolerance: float = 0.20
    recurring_upcoming_days: int = 7
    recurring_missing_grace_days: int = 5
    recurring_lookback_days: int = 180
    recurring_min_occurrences: int = 2

    # Manual recurring expenses: "Netflix|15.99|15,Spotify|9.99|20"
    # Format per entry: "Merchant Name|monthly_amount|day_of_month"
    recurring_expenses: str = ""

    # Merchants to exclude from auto-detection (comma-separated, case-insensitive)
    recurring_exclude_merchants: str = ""

    # Cashflow: categories treated as internal transfers, excluded from income/expense
    cashflow_transfer_categories: str = "TRANSFER_IN,TRANSFER_OUT"

    # Merchants whose TRANSFER_OUT transactions are internal savings moves —
    # money still belongs to you, just in a different account.
    # These are shown in the budget as "Savings" and excluded from the deficit.
    # Example: Oportun pulls cash into goal-savings buckets; it's not an outflow.
    savings_transfer_merchants: str = ""

    def get_savings_transfer_merchants(self) -> set[str]:
        return {m.strip().lower() for m in self.savings_transfer_merchants.split(",") if m.strip()}

    # Rentcast AVM API -- optional, used by `fintrack assets property refresh`
    # Free tier at rentcast.io (~50 requests/month). Leave blank to use manual values only.
    rentcast_api_key: str = ""

    # Optional -- only needed when classification/llm.py is added
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-4-8"

    # Helpers
    def get_plaid_products(self) -> list[str]:
        return [p.strip() for p in self.plaid_products.split(",") if p.strip()]

    def get_classifier_chain(self) -> list[str]:
        return [n.strip() for n in self.classifier_chain.split(",") if n.strip()]

    def get_cashflow_transfer_categories(self) -> frozenset:
        return frozenset(c.strip() for c in self.cashflow_transfer_categories.split(",") if c.strip())

    def get_recurring_exclude_merchants(self) -> set:
        return {m.strip().lower() for m in self.recurring_exclude_merchants.split(",") if m.strip()}

    def get_recurring_expenses(self) -> list[tuple]:
        """Parse 'Netflix|15.99|15' entries into (name, amount, day) tuples."""
        result = []
        for entry in self.recurring_expenses.split(","):
            parts = [p.strip() for p in entry.strip().split("|")]
            if len(parts) == 3:
                try:
                    result.append((parts[0], float(parts[1]), int(parts[2])))
                except ValueError:
                    pass
        return result


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
