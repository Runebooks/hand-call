"""
Freshworks product definitions and filter helpers.

Ported from the n8n FRESH_PRODUCTS constant in Code: Resolve ticket & context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

FRESH_PRODUCTS = [
    {"key": "freshservice", "label": "Freshservice", "aliases": ["freshservice", "fresh service"]},
    {"key": "freshdesk", "label": "Freshdesk", "aliases": ["freshdesk", "fresh desk"]},
    {"key": "freshchat", "label": "Freshchat", "aliases": ["freshchat", "fresh chat"]},
    {"key": "freshcaller", "label": "Freshcaller", "aliases": ["freshcaller", "fresh caller"]},
    {"key": "freshsales", "label": "Freshsales", "aliases": ["freshsales", "fresh sales"]},
    {"key": "freshmarketer", "label": "Freshmarketer", "aliases": ["freshmarketer", "fresh marketer"]},
    {"key": "freshrelease", "label": "Freshrelease", "aliases": ["freshrelease", "fresh release"]},
    {"key": "freshstatus", "label": "Freshstatus", "aliases": ["freshstatus", "fresh status"]},
    {"key": "freshworks", "label": "Freshworks", "aliases": ["freshworks"]},
]


@dataclass
class ProductFilter:
    key: str
    label: str
    match_patterns: list[str] = field(default_factory=list)


def _build_filter(product: dict) -> ProductFilter:
    patterns: set[str] = set()
    patterns.add(product["key"])
    for alias in product.get("aliases", []):
        patterns.add(alias.lower().replace(" ", ""))
        patterns.add(alias.lower().strip())
    return ProductFilter(
        key=product["key"],
        label=product["label"],
        match_patterns=[p for p in patterns if p],
    )


def parse_product_filter(text: str) -> Optional[ProductFilter]:
    """Find the longest-matching Freshworks product name in user text."""
    lower = (text or "").lower()
    best: Optional[ProductFilter] = None
    best_len = 0
    for prod in FRESH_PRODUCTS:
        for alias in prod.get("aliases", []):
            try:
                if re.search(r"\b" + re.escape(alias) + r"\b", lower) and len(alias) > best_len:
                    best = _build_filter(prod)
                    best_len = len(alias)
            except re.error:
                pass
    return best


def row_matches_product(row: dict, pf: Optional[ProductFilter]) -> bool:
    """Return True when row's product/mim_type field matches the filter."""
    if pf is None:
        return True
    blob = _product_blob(row)
    if not blob:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", blob)
    for pat in pf.match_patterns:
        p = pat.lower()
        pc = re.sub(r"[^a-z0-9]+", "", p)
        if blob.__contains__(p) or (len(pc) >= 4 and compact.__contains__(pc)):
            return True
    return False


def _product_blob(row: dict) -> str:
    parts = []
    for k, v in row.items():
        if v is None or v == "":
            continue
        lk = k.lower()
        if lk in ("product", "products", "mim_type", "mim type", "impacted_product",
                   "service", "product_name"):
            parts.append(str(v))
    return " ".join(parts).lower()


def known_product_labels() -> str:
    return ", ".join(p["label"] for p in FRESH_PRODUCTS)
