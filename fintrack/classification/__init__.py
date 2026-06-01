"""
Classification pipeline for fintrack.

Classifier chain resolution order (configured via CLASSIFIER_CHAIN env var):

  llm   → fintrack/classification/llm.py  (create this file to enable)
  rules → RulesClassifier  (fast regex, no external deps)
  plaid → PlaidClassifier  (Plaid's personal_finance_category, always available)

To add an LLM classifier:
  1. Create fintrack/classification/llm.py
  2. Implement class LLMClassifier(TransactionClassifier)
  3. Set CLASSIFIER_CHAIN=llm,rules,plaid in your .env

The build_chain() function below will pick it up automatically.
"""

from .base import ClassificationResult, ClassifierChain, TransactionClassifier
from .plaid import PlaidClassifier
from .rules import RulesClassifier

__all__ = [
    "ClassificationResult",
    "ClassifierChain",
    "TransactionClassifier",
    "PlaidClassifier",
    "RulesClassifier",
    "build_chain",
]


def build_chain(names: list[str]) -> ClassifierChain:
    """
    Build a ClassifierChain from an ordered list of classifier names.
    Unknown names are skipped with a warning so a misconfigured LLM key
    doesn't break the whole pipeline.
    """
    import warnings

    classifiers: list[TransactionClassifier] = []

    for name in names:
        if name == "rules":
            classifiers.append(RulesClassifier())

        elif name == "plaid":
            classifiers.append(PlaidClassifier())

        elif name == "llm":
            # ── LLM hook ──────────────────────────────────────────────────
            # Drop fintrack/classification/llm.py with LLMClassifier to enable.
            # The import is deferred so missing LLM deps don't affect other paths.
            try:
                from .llm import LLMClassifier  # type: ignore[import]
                classifiers.append(LLMClassifier())
            except ImportError:
                warnings.warn(
                    "Classifier 'llm' requested but fintrack/classification/llm.py "
                    "not found (or its deps are missing). Skipping.",
                    stacklevel=2,
                )
        else:
            warnings.warn(f"Unknown classifier '{name}' in CLASSIFIER_CHAIN — skipping.", stacklevel=2)

    if not classifiers:
        warnings.warn(
            "No valid classifiers found; falling back to PlaidClassifier.",
            stacklevel=2,
        )
        classifiers.append(PlaidClassifier())

    return ClassifierChain(classifiers)
