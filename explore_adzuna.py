"""
Scratch script for poking at the Adzuna API and seeing what it returns.

Setup:
    1. Get an app_id / app_key at https://developer.adzuna.com/
    2. Copy .env.example to .env and fill in ADZUNA_APP_ID / ADZUNA_APP_KEY
    3. uv run explore_adzuna.py
"""

import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

APP_ID = os.environ["ADZUNA_APP_ID"]
APP_KEY = os.environ["ADZUNA_APP_KEY"]
COUNTRY = os.environ.get("ADZUNA_COUNTRY", "gb")

BASE_URL = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}"


def search(what="python developer", where="", page=1, results_per_page=10, **extra_params):
    """Hit the /search/{page} endpoint and return the parsed JSON."""
    url = f"{BASE_URL}/search/{page}"
    params = {
        "app_id": APP_ID,
        "app_key": APP_KEY,
        "what": what,
        "where": where,
        "salary_min": 100000,
        "max_days_old": 14,
        "company": "amazon",
        "results_per_page": results_per_page,
        **extra_params,
    }
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def categories():
    """Hit the /categories endpoint and return the parsed JSON."""
    url = f"{BASE_URL}/categories"
    params = {"app_id": APP_ID, "app_key": APP_KEY}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def histogram(what="python developer", where=""):
    """Hit the /histogram endpoint (salary distribution) and return the parsed JSON."""
    url = f"{BASE_URL}/histogram"
    params = {"app_id": APP_ID, "app_key": APP_KEY, "what": what, "where": where}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def top_companies(what="python developer"):
    """Hit the /top_companies endpoint and return the parsed JSON."""
    url = f"{BASE_URL}/top_companies"
    params = {"app_id": APP_ID, "app_key": APP_KEY, "what": what}
    response = requests.get(url, params=params)
    response.raise_for_status()
    return response.json()


def pretty(data):
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    results = search(what="data scientist", where="united states", results_per_page=5)
    print(f"count: {results.get('count')}")
    print(f"top-level keys: {list(results.keys())}")

    jobs = results.get("results", [])
    if jobs:
        print(f"\nkeys per job result: {list(jobs[0].keys())}")
        print("\nfirst job:")
        pretty(jobs[0])
