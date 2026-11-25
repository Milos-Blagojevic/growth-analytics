"""Greenhouse public job-board API (no key needed). Lists only open, published postings, with their text."""

import html

import requests

HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")  # boards on the EU hosts answer on an API not known here
API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}


def fetch_board(board):
    """Returns (state, jobs): state is "ok", "missing" (no such board) or "unknown" (request/API error).

    jobs is a list of the API's job dicts (id, title, absolute_url, location, content, ...).
    """
    try:
        response = requests.get(API.format(board=board), params={"content": "true"}, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return "unknown", []
    if response.status_code == 404:
        return "missing", []
    if response.status_code != 200:
        return "unknown", []
    try:
        return "ok", [job for job in response.json()["jobs"] if isinstance(job, dict)]
    except (ValueError, KeyError, TypeError):
        return "unknown", []


def posting(job):
    """One job of fetch_board as {"id", "title", "url", "location", "workplace", "html"}. The link is the API's
    `absolute_url`, which can be a careers page that embeds the board (careers.acme.com/...?gh_jid=<id>). Greenhouse
    has no work-type field: "unknown"."""
    location = job.get("location")
    return {
        "id": str(job.get("id") or ""),
        "title": str(job.get("title") or ""),
        "url": str(job.get("absolute_url") or ""),
        "location": str(location.get("name") or "") if isinstance(location, dict) else "",
        "workplace": "unknown",
        "html": html.unescape(str(job.get("content") or "")),  # the API escapes the posting's HTML once more
    }
