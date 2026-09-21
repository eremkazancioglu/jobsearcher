"""
Shared contract for per-platform digest email parsers.

Subclass DigestParser and set PLATFORM_NAME/SENDER_QUERY -- subclasses
register themselves via __init_subclass__ the moment their module is
imported. agents/digest_parsers/__init__.py imports every module in this
package automatically, so a new platform is picked up just by adding a
file here -- nothing in agents/digest_source.py has to change.
"""

import hashlib
import re
from abc import ABC, abstractmethod

from pydantic import BaseModel


class ParsedListing(BaseModel):
    title: str
    company: str
    platform: str  # logging only -- postings.source is unified 'email_digest'
    location: str | None = None  # extra disambiguating signal for confirm+extract, when the digest shows one


class DigestParser(ABC):
    PLATFORM_NAME: str
    SENDER_QUERY: str  # Gmail search fragment, e.g. "from:jobalerts-noreply@linkedin.com"

    _registry: list[type["DigestParser"]] = []

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        DigestParser._registry.append(cls)

    @abstractmethod
    def parse(self, email_html: str) -> list[ParsedListing]:
        """Extract (title, company) pairs from one digest email's HTML body."""


def all_parsers() -> list[type[DigestParser]]:
    return list(DigestParser._registry)


def normalized_listing_id(title: str, company: str) -> str:
    """Stable hash used both for pooling/deduping listings across
    platforms and as postings.external_id (source='email_digest'). Known
    imperfection: title text reformatted beyond casing/whitespace (e.g.
    "Sr." vs "Senior") normalizes to a different hash and is treated as a
    new listing -- see CLAUDE.md's Phase 4 section."""
    normalized = re.sub(r"[^a-z0-9]+", " ", f"{title} {company}".lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()
