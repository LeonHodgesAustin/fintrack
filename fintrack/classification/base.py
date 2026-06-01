from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ClassificationResult:
    category: str
    subcategory: str
    confidence: float  # 0.0 – 1.0
    source: str        # "plaid" | "rules" | "llm" | "fallback"


class TransactionClassifier(ABC):
    """
    Single-transaction classifier interface.

    Return None if this classifier cannot make a determination — the
    ClassifierChain will then try the next classifier in the pipeline.
    Returning None is the correct signal for "I don't know", not for
    returning a low-confidence result; confidence is metadata on a real answer.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def classify(self, transaction: dict) -> ClassificationResult | None:
        """
        Classify a single transaction dict (as returned by to_dict() on a
        Plaid Transaction model).  Return None to pass to the next classifier.
        """
        ...


_FALLBACK = ClassificationResult(
    category="UNCATEGORIZED",
    subcategory="",
    confidence=0.0,
    source="fallback",
)


class ClassifierChain:
    """
    Try classifiers in order; use the first non-None result.

    To add an LLM classifier, prepend it to the list passed here, or
    update classification/__init__.py:build_chain() to instantiate it
    when "llm" appears in the chain config.

    Example pipeline: [LLMClassifier(), RulesClassifier(), PlaidClassifier()]
    """

    def __init__(self, classifiers: list[TransactionClassifier]) -> None:
        self._classifiers = classifiers

    def classify(self, transaction: dict) -> ClassificationResult:
        for classifier in self._classifiers:
            result = classifier.classify(transaction)
            if result is not None:
                return result
        return _FALLBACK
