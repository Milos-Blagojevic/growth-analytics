"""Lever public postings API (no key needed). Lists only open, published postings, with their text."""

import requests

HOSTS = {"jobs.lever.co": "https://api.lever.co", "jobs.eu.lever.co": "https://api.eu.lever.co"}  # board host: API
API = "{api}/v0/postings/{board}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
WORKPLACES = {"remote": "remote", "hybrid": "hybrid", "onsite": "on-site"}


def fetch_board(board, *, eu=False):
    """Returns (state, jobs): state is "ok", "missing" (no such board) or "unknown" (request/API error).

    jobs is a list of the API's posting dicts (id, text, hostedUrl, categories, workplaceType, description, lists,
    ...). eu=True asks the API of the EU-hosted boards (jobs.eu.lever.co).
    """
    api = HOSTS["jobs.eu.lever.co" if eu else "jobs.lever.co"]
    try:
        response = requests.get(API.format(api=api, board=board), params={"mode": "json"}, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return "unknown", []
    if response.status_code == 404:
        return "missing", []
    if response.status_code != 200:
        return "unknown", []
    try:
        jobs = response.json()
    except ValueError:
        return "unknown", []
    if not isinstance(jobs, list):
        return "unknown", []
    return "ok", [job for job in jobs if isinstance(job, dict)]


def posting(job):
    """One posting of fetch_board as {"id", "title", "url", "location", "workplace", "html"}: workplace is "remote",
    "hybrid", "on-site" or "unknown"; html is the description, its lists and the closing text."""
    categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
    places = categories.get("allLocations") or [categories.get("location")]
    lists = [part for part in job.get("lists") or [] if isinstance(part, dict)]
    return {
        "id": str(job.get("id") or ""),
        "title": str(job.get("text") or ""),
        "url": str(job.get("hostedUrl") or ""),
        "location": "; ".join(place for place in places if isinstance(place, str) and place.strip()),
        "workplace": WORKPLACES.get(job.get("workplaceType"), "unknown"),
        "html": "".join([
            str(job.get("description") or ""),
            *(f"<h3>{part.get('text') or ''}</h3><ul>{part.get('content') or ''}</ul>" for part in lists),
            str(job.get("additional") or ""),
        ]),  # fmt: skip
    }
