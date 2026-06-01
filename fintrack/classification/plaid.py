from .base import ClassificationResult, TransactionClassifier


_CONFIDENCE_MAP = {
    "VERY_HIGH": 0.95,
    "HIGH": 0.80,
    "MEDIUM": 0.60,
    "LOW": 0.40,
    "UNKNOWN": 0.25,
}


class PlaidClassifier(TransactionClassifier):
    """
    Pass-through classifier that surfaces Plaid's own personal_finance_category.

    This is an excellent fallback: it covers every transaction Plaid has seen,
    but has lower precision than merchant-specific rules and no custom taxonomy.
    """

    @property
    def name(self) -> str:
        return "plaid"

    def classify(self, transaction: dict) -> ClassificationResult | None:
        pfc = transaction.get("personal_finance_category")
        if not pfc:
            return None

        primary = pfc.get("primary") or "UNCATEGORIZED"
        detailed = pfc.get("detailed") or ""
        confidence_level = pfc.get("confidence_level") or "UNKNOWN"
        confidence = _CONFIDENCE_MAP.get(confidence_level, 0.25)

        return ClassificationResult(
            category=primary,
            subcategory=detailed,
            confidence=confidence,
            source="plaid",
        )
