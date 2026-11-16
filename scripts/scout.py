#!/usr/bin/env python3
"""Find recent resources that may belong in the list and report them for review.

Candidates come from free, keyless sources: the arXiv API, the GitHub search
API, and the RSS or Atom feeds of the blogs already in the list. Anything that
is already linked from the README, or that an earlier scout issue already
reported, is dropped. A model reached through an OpenAI-compatible API (Gemini by default) then reads each candidate's
title and abstract and keeps only those that look substantive and in scope. The
model never browses and never edits the list: it only answers keep or drop for
text this script fetched, and a maintainer decides what to add.

    GITHUB_TOKEN=... SCOUT_API_KEY=... GITHUB_REPOSITORY=owner/repo python3 scripts/scout.py --out scout-report.md

The script always exits 0 when it could run. When $GITHUB_OUTPUT is set it
writes `candidates` there so a workflow can decide whether to open an issue.
If the model is unavailable the report lists the candidates unfiltered and says
so.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

USER_AGENT = "awesome-safety-critical-ai-scout"
# Any OpenAI-compatible chat endpoint works. The defaults are Google's Gemini API,
# which has a free tier; override them with SCOUT_API_URL and SCOUT_MODEL.
API_URL = os.environ.get("SCOUT_API_URL") or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
# A comma-separated list: each model is tried in turn until one answers.
# gemini-flash-latest timed out for this key; these pinned models passed the probe.
DEFAULT_MODEL = "gemini-3.5-flash,gemini-3.1-flash-lite"
# The free tier allows only a handful of requests a minute.
MODEL_PAUSE = 7
SCOUT_LABEL = "scout"

LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
URL_ANYWHERE = re.compile(r"https?://[^\s)>\]\"']+")
ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5})(?:v[0-9]+)?", re.I)

ARXIV_QUERIES = [
    '(abs:"safety-critical" OR abs:"safety critical") AND (abs:"machine learning" OR abs:"neural network"'
    ' OR abs:"neural networks" OR abs:LLM OR abs:LLMs OR abs:"language model" OR abs:"language models" OR abs:"deep learning")',
    '(abs:"safety assurance" OR abs:"safety case" OR abs:"assurance case" OR abs:certification OR abs:"runtime monitoring")'
    ' AND (abs:"machine learning" OR abs:"neural network" OR abs:"neural networks" OR abs:LLM OR abs:"language model" OR abs:"deep learning")',
    '(abs:"neural network verification" OR abs:"formal verification" OR abs:"out-of-distribution detection"'
    ' OR abs:"uncertainty estimation") AND (abs:"safety-critical" OR abs:avionics OR abs:"autonomous driving" OR abs:"medical")',
    '(abs:SOTIF OR abs:"ISO 26262" OR abs:"DO-178C" OR abs:"EASA" OR abs:"IEC 61508" OR abs:"ISO 21448" OR abs:"UL 4600")'
    ' AND (abs:"machine learning" OR abs:AI OR abs:"neural network" OR abs:LLM)',
]
GITHUB_QUERIES = [
    "safety-critical machine learning in:description,readme",
    "neural network verification in:name,description",
    "ml safety assurance in:description",
    "out-of-distribution detection safety in:description",
    "ai safety certification in:description",
]
FEEDS = {
    "Pivot to AI": "https://pivot-to-ai.com/feed/",
    "Susana Cox": "https://disesdi.substack.com/feed",
    "Clear Prop": "https://pamirsevincel.substack.com/feed",
    "DeepMind Safety Research": "https://deepmindsafetyresearch.medium.com/feed",
    "Bits & Chips": "https://bits-chips.com/feed/",
}

# Keep these in step with the Key Considerations in CONTRIBUTING.
RUBRIC = """\
You screen candidate references for an awesome list about safety-critical AI:
building, assuring, certifying, and operating AI and machine learning systems
where failures can harm people, such as aviation, automotive, medical, rail,
space, and energy. In scope: safety assurance and certification of ML, standards
and guidelines, verification and validation, robustness, uncertainty, runtime
monitoring, adversarial attacks and defenses, safety cases, incident analyses,
and open-source tools and datasets for these.

Answer keep=false for anything that is:
- marketing, a product announcement, funding news, or press coverage;
- a listicle, a trend piece, or padded writing with bizspeak and little content;
- a claim with no checkable artifact such as code, data, a method, or a source;
- general AI ethics, policy, or hype with no engineering substance;
- machine learning applied to a domain with no safety, assurance, or reliability angle.

Answer keep=true only when the text points to substantive work that clearly
fits. When in doubt, answer keep=false. Judge only the text you are given and
never assume facts beyond it.

Reply with JSON only: a list of {"id": "<id>", "keep": true|false, "reason": "<at most 15 words>"}, one object per candidate.
"""


@dataclass
class Candidate:
    id: str
    title: str
    url: str
    kind: str  # paper, repo, post
    summary: str
    date: str
    source: str
    verdict: Optional[bool] = None
    reason: str = ""


# --------------------------------------------------------------------------
# Known resources


def normalize(url: str) -> str:
    """Reduce a URL to a key that matches its other spellings."""
    url = url.strip().rstrip(".,;")
    match = ARXIV_ID.search(url)
    if match:
        return f"arxiv:{match.group(1)}"
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host == "github.com":
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            name = parts[1].removesuffix(".git")
            return f"github:{parts[0].lower()}/{name.lower()}"
    if host in ("doi.org", "dx.doi.org"):
        return f"doi:{path.lstrip('/').lower()}"
    return f"{host}{path}".lower()


def known_keys(*texts: str) -> set[str]:
    keys: set[str] = set()
    for text in texts:
        for url in URL_ANYWHERE.findall(text):
            keys.add(normalize(url))
    return keys


# --------------------------------------------------------------------------
# HTTP


def fetch(url: str, headers: Optional[dict] = None, data: Optional[bytes] = None,
          timeout: int = 30, retry_on: tuple = (), pauses: tuple = (15, 45, 90)) -> Optional[bytes]:
    """GET or POST a URL. Statuses in retry_on (say 429 and 503) are retried after each pause."""
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    waits = list(pauses)
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = " ".join(error.read()[:300].decode("utf-8", "replace").split())
            if error.code in retry_on and waits:
                print(f"warning: {url}: HTTP {error.code}, retrying in {waits[0]}s", file=sys.stderr)
                time.sleep(waits.pop(0))
                continue
            print(f"warning: {url}: HTTP {error.code}: {detail}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt >= 3:
                print(f"warning: {url}: {error}", file=sys.stderr)
                return None


def github_get(path: str, token: str) -> Optional[object]:
    body = fetch(
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    return json.loads(body) if body else None


# --------------------------------------------------------------------------
# Sources


ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv(xml_text: str) -> list[Candidate]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for entry in root.findall(f"{ATOM}entry"):
        link = (entry.findtext(f"{ATOM}id") or "").strip()
        match = ARXIV_ID.search(link)
        if not match:
            continue
        out.append(
            Candidate(
                id=f"arxiv:{match.group(1)}",
                title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
                url=f"https://arxiv.org/abs/{match.group(1)}",
                kind="paper",
                summary=" ".join((entry.findtext(f"{ATOM}summary") or "").split()),
                date=(entry.findtext(f"{ATOM}published") or "")[:10],
                source="arXiv",
            )
        )
    return out


def from_arxiv(days: int, sleep: Callable[[float], None] = time.sleep) -> list[Candidate]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    found: dict[str, Candidate] = {}
    for i, query in enumerate(ARXIV_QUERIES):
        if i:
            sleep(3)  # arXiv asks for one request every three seconds
        params = urllib.parse.urlencode(
            {"search_query": query, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": 50}
        )
        body = fetch(f"https://export.arxiv.org/api/query?{params}")
        if not body:
            continue
        for cand in parse_arxiv(body.decode("utf-8", "replace")):
            if cand.date >= since:
                found.setdefault(cand.id, cand)
    return list(found.values())


def from_github(days: int, token: str) -> list[Candidate]:
    # Brand-new repositories with no stars are almost all noise, so ask for
    # the 100 stars CONTRIBUTING requires and give projects a year to earn them.
    created = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    pushed = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    found: dict[str, Candidate] = {}
    for query in GITHUB_QUERIES:
        q = urllib.parse.quote(f"{query} pushed:>={pushed} created:>={created} stars:>=100 fork:false")
        data = github_get(f"/search/repositories?q={q}&sort=updated&per_page=20", token)
        for repo in (data or {}).get("items", []):
            if repo.get("archived"):
                continue
            cid = normalize(repo["html_url"])
            found.setdefault(
                cid,
                Candidate(
                    id=cid,
                    title=repo["full_name"],
                    url=repo["html_url"],
                    kind="repo",
                    summary=(repo.get("description") or "")
                    + f" ({repo.get('stargazers_count', 0)} stars, {repo.get('language') or 'no language'})",
                    date=(repo.get("created_at") or "")[:10],
                    source="GitHub",
                ),
            )
        time.sleep(2)  # the search API allows few requests per minute
    return list(found.values())


def parse_feed(xml_text: str, source: str) -> list[Candidate]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    items = [(i, "rss") for i in root.iter("item")] + [(e, "atom") for e in root.iter(f"{ATOM}entry")]
    for node, flavor in items:
        if flavor == "rss":
            title = node.findtext("title") or ""
            link = node.findtext("link") or ""
            summary = node.findtext("description") or ""
            date_text = node.findtext("pubDate") or node.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        else:
            title = node.findtext(f"{ATOM}title") or ""
            link_node = node.find(f"{ATOM}link")
            link = (link_node.get("href") if link_node is not None else "") or ""
            summary = node.findtext(f"{ATOM}summary") or node.findtext(f"{ATOM}content") or ""
            date_text = node.findtext(f"{ATOM}published") or node.findtext(f"{ATOM}updated") or ""
        if not link:
            continue
        summary = re.sub(r"<[^>]+>", " ", summary)
        out.append(
            Candidate(
                id=normalize(link),
                title=" ".join(title.split()),
                url=link.strip(),
                kind="post",
                summary=" ".join(summary.split())[:1200],
                date=parse_date(date_text),
                source=source,
            )
        )
    return out


def parse_date(text: str) -> str:
    text = text.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text[:10]


def from_feeds(days: int) -> list[Candidate]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = []
    for name, url in FEEDS.items():
        body = fetch(url)
        if not body:
            continue
        out += [c for c in parse_feed(body.decode("utf-8", "replace"), name) if c.date >= since]
    return out


def previously_reported(repo: str, token: str) -> set[str]:
    """Keys of every URL in issues and pull requests that carry the scout label."""
    keys: set[str] = set()
    for page in range(1, 6):
        data = github_get(f"/repos/{repo}/issues?labels={SCOUT_LABEL}&state=all&per_page=100&page={page}", token)
        if not data:
            break
        for issue in data:
            keys |= known_keys(issue.get("title") or "", issue.get("body") or "")
        if len(data) < 100:
            break
    return keys


# --------------------------------------------------------------------------
# Model filter


def build_messages(batch: list[Candidate]) -> list[dict]:
    lines = []
    for cand in batch:
        lines.append(
            f'id: {cand.id}\nkind: {cand.kind}\nsource: {cand.source}\ntitle: {cand.title}\n'
            f'text: {cand.summary[:1200]}\n'
        )
    return [
        {"role": "system", "content": RUBRIC},
        {"role": "user", "content": "Candidates:\n\n" + "\n".join(lines)},
    ]


def parse_verdicts(text: str) -> dict[str, tuple[bool, str]]:
    """Read the model's JSON, tolerating a code fence or chatter around it."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return {}
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    out = {}
    for item in items:
        if isinstance(item, dict) and "id" in item and isinstance(item.get("keep"), bool):
            out[str(item["id"])] = (item["keep"], str(item.get("reason", ""))[:200])
    return out


def chat(messages: list[dict], api_key: str, models: str) -> Optional[str]:
    """Return the text of the first answer from the comma-separated models, trying each in order."""
    names = [m.strip() for m in models.split(",") if m.strip()]
    for position, model in enumerate(names):
        payload = json.dumps({"model": model, "messages": messages, "temperature": 0}).encode()
        # With a fallback waiting, give up on a busy model sooner.
        pauses = (15, 45, 90) if len(names) == 1 or position == len(names) - 1 else (15,)
        body = fetch(
            API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=payload,
            timeout=120,
            retry_on=(429, 500, 502, 503, 504),
            pauses=pauses,
        )
        if not body:
            print(f"warning: {model} did not answer; {'trying the next model' if position < len(names) - 1 else 'no models left'}", file=sys.stderr)
            continue
        try:
            content = json.loads(body)["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            print(f"warning: unexpected response from {model}: {body[:300]!r}", file=sys.stderr)
            continue
        if position:
            print(f"note: answered by the fallback model {model}", file=sys.stderr)
        return content
    return None


def ask_model(batch: list[Candidate], token: str, model: str) -> Optional[dict[str, tuple[bool, str]]]:
    content = chat(build_messages(batch), token, model)
    if content is None:
        return None
    verdicts = parse_verdicts(content)
    if not verdicts:
        print(f"warning: no verdicts in the model's answer: {content[:300]!r}", file=sys.stderr)
    return verdicts


def filter_with_model(cands: list[Candidate], token: str, model: str, batch_size: int = 8) -> bool:
    """Set verdicts in place. Returns False if the model could not judge everything."""
    complete = True
    for i in range(0, len(cands), batch_size):
        batch = cands[i : i + batch_size]
        verdicts = ask_model(batch, token, model)
        if verdicts is None:
            complete = False
            continue
        by_key = {k.strip().lower(): v for k, v in verdicts.items()}
        for cand in batch:
            found = by_key.get(cand.id.strip().lower())
            if found is not None:
                cand.verdict, cand.reason = found
            else:
                print(f"warning: the model gave no verdict for {cand.id}", file=sys.stderr)
                complete = False
        time.sleep(MODEL_PAUSE)
    return complete


# --------------------------------------------------------------------------
# Report


def render(cands: list[Candidate], filtered: bool, dropped: int, days: int, model: str) -> str:
    keep = [c for c in cands if c.verdict is not False]
    lines = [
        f"Candidates from the last {days} days that are not already in the list, "
        f"reported by an earlier scout issue, or dropped by the filter.",
        "",
    ]
    if not filtered:
        lines += [
            "> The model filter was unavailable or incomplete, so some entries below are **unfiltered**. "
            "Expect noise.",
            "",
        ]
    else:
        lines += [f"Screened by `{model}`; {dropped} candidates were dropped.", ""]
    lines += [
        "Nothing here is verified. Open each link, check the claims against the source, and read "
        "`CONTRIBUTING` before proposing an entry. One resource per pull request.",
        "",
    ]
    for kind, heading in (("paper", "Papers"), ("repo", "Repositories"), ("post", "Posts")):
        group = sorted((c for c in keep if c.kind == kind), key=lambda c: c.date, reverse=True)
        if not group:
            continue
        lines += [f"### {heading}", ""]
        for cand in group:
            note = f" Filter: {cand.reason}" if cand.verdict and cand.reason else ""
            lines.append(f"- [{cand.title}]({cand.url}) - {cand.source}, {cand.date}.{note}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def gather(days: int, token: str, known: set[str]) -> list[Candidate]:
    everything = from_arxiv(days) + from_github(days, token) + from_feeds(days)
    seen: set[str] = set()
    out = []
    for cand in everything:
        key = normalize(cand.url)
        if key in known or cand.id in known or key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--contributing", default="CONTRIBUTING")
    parser.add_argument("--out", default="scout-report.md")
    parser.add_argument("--days", type=int, default=10, help="how far back to look")
    parser.add_argument("--model", default=os.environ.get("SCOUT_MODEL") or DEFAULT_MODEL)
    parser.add_argument("--max-candidates", type=int, default=60, help="cap on candidates sent to the model")
    parser.add_argument("--no-model", action="store_true", help="skip the model filter")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    with open(args.readme, encoding="utf-8") as f:
        readme = f.read()
    with open(args.contributing, encoding="utf-8") as f:
        contributing = f.read()
    known = known_keys(readme, contributing)
    if repo:
        known |= previously_reported(repo, token)

    cands = gather(args.days, token, known)
    cands.sort(key=lambda c: c.date, reverse=True)
    cands = cands[: args.max_candidates]

    filtered = False
    api_key = os.environ.get("SCOUT_API_KEY", "")
    if cands and not args.no_model:
        if api_key:
            filtered = filter_with_model(cands, api_key, args.model)
        else:
            print("warning: SCOUT_API_KEY is not set, so the model filter was skipped", file=sys.stderr)
    dropped = sum(1 for c in cands if c.verdict is False)
    kept = [c for c in cands if c.verdict is not False]

    report = render(cands, filtered, dropped, args.days, args.model)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"{len(kept)} candidates kept, {dropped} dropped, {len(cands)} considered")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"candidates={len(kept)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
