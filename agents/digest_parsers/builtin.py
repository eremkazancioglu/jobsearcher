"""
BuiltIn "New Job Matches" digest parser.

Confirmed against a real digest email: each job entry is an <a> whose
href contains "/job/" (wrapped in an awstrack.me tracking redirect --
not used here, only title/company are extracted), containing a
font-size:16px <div> (company) followed by a font-weight:700 <div>
(title). No class names on this template, only inline styles -- more
fragile to a template change than LinkedIn's class-based markup, worth
re-checking if this starts returning nothing.

Duplicate entries for the same posting are expected (BuiltIn wraps the
logo, title, and a "view job" region in separate nested <a> tags all
pointing at the same job) -- not deduped here, since the pooled
cross-platform dedup in agents/digest_source.py already collapses them
by the same normalized hash.
"""

from bs4 import BeautifulSoup

from .base import DigestParser, ParsedListing


class BuiltInDigestParser(DigestParser):
    PLATFORM_NAME = "builtin"
    SENDER_QUERY = "from:support@builtin.com"

    def parse(self, email_html: str) -> list[ParsedListing]:
        soup = BeautifulSoup(email_html, "html.parser")
        listings = []

        job_links = [a for a in soup.find_all("a", href=True) if "%2Fjob%2F" in a["href"]]

        for a in job_links:
            divs = a.find_all("div")
            company_div = next(
                (d for d in divs if "font-size:16px" in (d.get("style") or "")), None
            )
            title_div = next(
                (d for d in divs if "font-weight:700" in (d.get("style") or "")), None
            )
            if not company_div or not title_div:
                continue

            company = company_div.get_text(strip=True)
            title = title_div.get_text(strip=True)
            if not company or not title:
                continue

            listings.append(
                ParsedListing(title=title, company=company, platform=self.PLATFORM_NAME)
            )

        return listings
