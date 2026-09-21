"""
Importing every module in this package is what triggers each DigestParser
subclass's __init_subclass__ registration (see base.py). Adding a new
digest platform later is "drop a new file here" -- this loop is what
makes that true, nothing else needs to change.
"""

import importlib
import pkgutil

for _, module_name, _ in pkgutil.iter_modules(__path__, prefix=f"{__name__}."):
    if module_name != f"{__name__}.base":
        importlib.import_module(module_name)

from .base import DigestParser, ParsedListing, all_parsers, normalized_listing_id  # noqa: E402

__all__ = ["DigestParser", "ParsedListing", "all_parsers", "normalized_listing_id"]
