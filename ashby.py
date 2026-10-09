"""Ashby public job-board API (no key needed). Lists only currently open, listed postings."""

import requests

HOST = "jobs.ashbyhq.com"
API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}


def fetch_board(board):
    """Returns (state, jobs): state is "ok", "missing" (no such board) or "unknown" (request/API error).

    jobs is a list of the API's job dicts (id, title, location, isRemote, jobUrl, publishedAt, ...).
    """
    try:
        response = requests.get(API.format(board=board), headers=HEADERS, timeout=15)
    except requests.RequestException:
        return "unknown", []
    if response.status_code == 404:
        return "missing", []
    if response.status_code != 200:
        return "unknown", []
    try:
        return "ok", list(response.json()["jobs"])
    except (ValueError, KeyError, TypeError):
        return "unknown", []
