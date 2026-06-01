import re
from typing import NamedTuple

from .base import ClassificationResult, TransactionClassifier


class _Rule(NamedTuple):
    pattern: re.Pattern
    category: str
    subcategory: str
    confidence: float


def _r(pattern: str, category: str, subcategory: str, confidence: float = 0.88) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), category, subcategory, confidence)


_RULES: list[_Rule] = [
    # Groceries
    _r(r"whole foods|trader joe|safeway|kroger|publix|aldi|wegmans|hyvee|meijer|stop.?shop|giant food|food lion|sprouts", "FOOD_AND_DRINK", "GROCERIES", 0.92),
    # Coffee
    _r(r"starbucks|dunkin|peets coffee|blue bottle|caribou coffee|tim horton", "FOOD_AND_DRINK", "COFFEE_SHOP", 0.92),
    # Food delivery
    _r(r"uber eats|doordash|grubhub|postmates|seamless|caviar|gopuff", "FOOD_AND_DRINK", "FOOD_DELIVERY", 0.92),
    # Restaurants (broad)
    _r(r"chipotle|mcdonald|burger king|wendy|chick.?fil|taco bell|subway|domino|papa john|pizza hut|panera|olive garden|applebee|cheesecake factory", "FOOD_AND_DRINK", "RESTAURANTS", 0.90),
    # Streaming / entertainment
    _r(r"netflix|hulu|spotify|disney\+|apple tv\+|hbo max|peacock|paramount\+|amazon prime|youtube premium|sling|fubo|crunchyroll|audible", "ENTERTAINMENT", "STREAMING", 0.95),
    # Rideshare
    _r(r"\buber\b(?!.*eats)|lyft\b", "TRANSPORTATION", "RIDESHARE", 0.92),
    # Gas stations
    _r(r"shell|chevron|\bbp\b|exxon|mobil|sunoco|valero|arco|circle k|wawa|sheetz|marathon", "TRANSPORTATION", "GAS_STATION", 0.90),
    # Airlines
    _r(r"delta air|united airlines|american airlines|southwest|jetblue|spirit airlines|frontier airlines|alaska airlines", "TRAVEL", "AIRLINE", 0.92),
    # Lodging
    _r(r"marriott|hilton|hyatt|ihg hotels|airbnb|vrbo|holiday inn|best western|hampton inn|doubletree", "TRAVEL", "LODGING", 0.92),
    # Pharmacy / drug stores
    _r(r"cvs|walgreens|rite aid|duane reade|costco pharmacy", "HEALTH", "PHARMACY", 0.88),
    # Gym / fitness
    _r(r"planet fitness|la fitness|equinox|ymca|anytime fitness|24 hour fitness|orangetheory|crunch fitness|peloton", "HEALTH", "GYM", 0.92),
    # Amazon (shopping — broad match last after food/delivery specifics above)
    _r(r"amazon(?!.*prime video)|amzn", "SHOPPING", "SHOPPING_ONLINE", 0.85),
    # Big-box retail
    _r(r"walmart|target\b|costco(?! pharma)|sam.?s club|home depot|lowe.?s|best buy|ikea|bed bath", "SHOPPING", "SHOPPING_GENERAL", 0.88),
    # Utilities / telecom
    _r(r"at&t|verizon|t-mobile|xfinity|spectrum|comcast|cox comm|con ed|pg&e|utility|electric|national grid", "UTILITIES", "UTILITIES_PHONE_AND_INTERNET", 0.85),
    # Insurance
    _r(r"geico|state farm|allstate|progressive|liberty mutual|nationwide|aaa insurance", "GENERAL_SERVICES", "INSURANCE", 0.88),
    # Investment / brokerage (Stash-specific keyword too)
    _r(r"stash\b|robinhood|fidelity|schwab|vanguard|e\*trade|td ameritrade|webull", "INCOME", "INVESTMENT", 0.88),
    # ATM / cash
    _r(r"\batm\b|cash withdrawal|zelle|venmo|cashapp|paypal", "TRANSFER_IN", "CASH_ADVANCE_AND_TRANSFER", 0.80),
]


class RulesClassifier(TransactionClassifier):
    """
    Fast, deterministic classifier driven by merchant-name regex patterns.
    Add more rules to _RULES above — no subclassing needed.
    """

    @property
    def name(self) -> str:
        return "rules"

    def classify(self, transaction: dict) -> ClassificationResult | None:
        name = transaction.get("merchant_name") or transaction.get("name") or ""
        for rule in _RULES:
            if rule.pattern.search(name):
                return ClassificationResult(
                    category=rule.category,
                    subcategory=rule.subcategory,
                    confidence=rule.confidence,
                    source="rules",
                )
        return None
