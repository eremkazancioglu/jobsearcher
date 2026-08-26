"""Postgres access -- psycopg v3, sync.

Two operations Phase 1 needs: check whether a posting's already on file
(the dedup-before-capture check) and insert a newly captured one. The
`unique(source, external_id)` constraint in schema.sql is the backstop if
these are ever raced; posting_exists() is what avoids re-running the
expensive capture flow for postings already written.
"""

import os

import psycopg
from dotenv import load_dotenv

from models.schema import Posting

load_dotenv(override=True)

DATABASE_URL = os.environ["DATABASE_URL"]


def posting_exists(source: str, external_id: str) -> bool:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select 1 from postings where source = %s and external_id = %s",
                (source, external_id),
            )
            return cur.fetchone() is not None


def insert_posting(posting: Posting) -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into postings (
                    source, external_id, title, company, location, url,
                    description, description_source, salary_min, salary_max,
                    salary_is_predicted
                ) values (
                    %(source)s, %(external_id)s, %(title)s, %(company)s,
                    %(location)s, %(url)s, %(description)s,
                    %(description_source)s, %(salary_min)s, %(salary_max)s,
                    %(salary_is_predicted)s
                )
                """,
                posting.model_dump(
                    include={
                        "source",
                        "external_id",
                        "title",
                        "company",
                        "location",
                        "url",
                        "description",
                        "description_source",
                        "salary_min",
                        "salary_max",
                        "salary_is_predicted",
                    }
                ),
            )
        conn.commit()
