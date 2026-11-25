"""Ashby public job-board API (no key needed). Lists only currently open postings, with their text."""

import requests

HOST = "jobs.ashbyhq.com"
API = "https://api.ashbyhq.com/posting-api/job-board/{board}"
WORKPLACES = {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "on-site"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}


def fetch_board(board):
    """Returns (state, jobs): state is "ok", "missing" (no such board) or "unknown" (request/API error).

    jobs is a list of the API's job dicts (id, title, location, isRemote, jobUrl, publishedAt, descriptionHtml, ...).
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


def posting(job):
    """One job of fetch_board as {"id", "title", "url", "location", "workplace", "html"}, or None for a job the board
    does not list (isListed false): every location it names, and "remote", "hybrid", "on-site" or "unknown"."""
    if job.get("isListed") is False:
        return None
    secondary = [place.get("location") for place in job.get("secondaryLocations") or [] if isinstance(place, dict)]
    workplace = WORKPLACES.get(job.get("workplaceType")) or ("remote" if job.get("isRemote") is True else "unknown")
    return {
        "id": str(job.get("id") or ""),
        "title": str(job.get("title") or ""),
        "url": str(job.get("jobUrl") or ""),
        "location": "; ".join(place for place in [job.get("location"), *secondary] if isinstance(place, str) and place.strip()),
        "workplace": workplace,
        "html": str(job.get("descriptionHtml") or ""),
    }
