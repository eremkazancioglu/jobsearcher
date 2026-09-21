"""
Wellfound "New job matches" digest parser.

Confirmed against real digest emails pulled live via the Gmail API, not
just the one initial sample -- Wellfound turned out to use at least two
different tags for the same job-title element (a <div> in one template, an
<h2> in another; the first live batch tested had a 2/5 miss rate on the
first version of this parser before that was found). Both variants share
the same inline style (font-weight: 700; font-size: 14px), which is what
this matches on instead of a tag name or class -- lambda-based, not a
plain style= substring match, so it works regardless of style-attribute
ordering. Company + employee-count follow as one text block in the next
sibling element (a <div> with two <span>s in one template, a plain <p> in
the other) -- splitting that block's text on "/" and taking the first
segment isolates the company name in both cases, since both templates use
"<company> / <employee count>" as the format either way. No class names on
this template, same fragility caveat as builtin.py -- this is more
brittle than LinkedIn's href-based selector and worth re-checking
periodically against real mail.
"""

import re

from bs4 import BeautifulSoup

from .base import DigestParser, ParsedListing

_TITLE_STYLE = re.compile(r"font-weight:\s*700").search
_TITLE_SIZE = re.compile(r"font-size:\s*14px").search


def _is_title_element(tag) -> bool:
    style = tag.get("style")
    return bool(style and _TITLE_STYLE(style) and _TITLE_SIZE(style))


class WellfoundDigestParser(DigestParser):
    PLATFORM_NAME = "wellfound"
    SENDER_QUERY = "from:team@hi.wellfound.com"

    def parse(self, email_html: str) -> list[ParsedListing]:
        soup = BeautifulSoup(email_html, "html.parser")
        listings = []

        for title_el in soup.find_all(_is_title_element):
            title = title_el.get_text(strip=True)
            if not title:
                continue

            next_el = title_el.find_next_sibling()
            if not next_el:
                continue

            company = next_el.get_text(" ", strip=True).split("/")[0].strip()
            if not company:
                continue

            listings.append(
                ParsedListing(title=title, company=company, platform=self.PLATFORM_NAME)
            )

        return listings
