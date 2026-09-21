"""
LinkedIn "Job Alerts" digest parser.

Confirmed against three real digest emails, live -- two from Gmail
samples (two separately configured alerts) and a third pulled directly
via the Gmail API. All three share the same structural pattern -- a job
entry's title is an <a href*="/jobs/view/"> wrapping plain text (no <img>
or nested <table> inside it -- those distinguish the logo-wrapper and
full-card-wrapper anchors LinkedIn also emits pointing at the same job),
with company + location following as the first <p> in that anchor's
containing <table>, formatted "Company · Location" -- but the *class name*
on that title anchor is not stable: confirmed two different values
("text-color-brand" vs "font-bold ... text-system-blue-50") across emails
that otherwise look like the same alert. Matching on href + element shape
instead of a class name is what survives that. Structural, not LLM-based
-- a known, bounded template, same category as tier 1's Adzuna JSON-LD
extraction in Phase 1. Will need revisiting if LinkedIn changes this
markup more substantially than a class rename.
"""

from bs4 import BeautifulSoup

from .base import DigestParser, ParsedListing


class LinkedInDigestParser(DigestParser):
    PLATFORM_NAME = "linkedin"
    SENDER_QUERY = "from:jobalerts-noreply@linkedin.com"

    def parse(self, email_html: str) -> list[ParsedListing]:
        soup = BeautifulSoup(email_html, "html.parser")
        listings = []

        for title_a in soup.find_all("a", href=True):
            if "/jobs/view/" not in title_a["href"]:
                continue
            if title_a.find("img") or title_a.find("table"):
                continue  # the logo-wrapper / full-card-wrapper anchors, not the title

            title = title_a.get_text(strip=True)
            if not title:
                continue

            table = title_a.find_parent("table")
            company_p = table.find("p") if table else None
            if not company_p:
                continue

            company_location = company_p.get_text(strip=True).split("·", maxsplit=1)
            company = company_location[0].strip()
            location = company_location[1].strip() if len(company_location) > 1 else None
            if not company:
                continue

            listings.append(
                ParsedListing(
                    title=title, company=company, platform=self.PLATFORM_NAME, location=location
                )
            )

        return listings
