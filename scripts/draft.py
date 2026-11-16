#!/usr/bin/env python3
"""Draft one README entry for a candidate from a scout issue.

A maintainer comments `/draft <url>` on a scout issue. This script fetches the
page itself, asks a model (Gemini by default) whether it belongs and, for tools,
to pick a subsection and write a short description, then checks the result
mechanically against the rules in CONTRIBUTING before touching the README. The
model never chooses where the line goes, never writes a paper's title or
authors, and never sees any file other than the page it was handed. If a check
fails the README is left alone and the report says why.

Three kinds of entry are supported, chosen by the URL:

    arxiv.org/abs/...     Articles       * (Smith *et al.*, 2026) [Title](url)
    github.com/org/repo   Tools          * [`org/repo`](url): description
    anything else         Blogs / News   * (Source, 2026) [Title](url)

    GITHUB_TOKEN=... SCOUT_API_KEY=... GITHUB_REPOSITORY=owner/repo COMMENT_BODY='/draft https://...' \\
        ISSUE_BODY='...' python3 scripts/draft.py --report draft-report.md

When $GITHUB_OUTPUT is set it writes `status` (ok or rejected), `branch`, and
`title` there for the workflow.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from scout import (
    ARXIV_ID,
    ATOM,
    DEFAULT_MODEL,
    chat,
    fetch,
    github_get,
    known_keys,
    normalize,
    parse_arxiv,
)

MAX_WORDS = 30
MIN_STARS = 100
MAX_IDLE_DAYS = 183
BANNED = (
    "blazing", "best", "revolutionary", "game-changing", "groundbreaking", "cutting-edge",
    "seamless", "powerful", "world-class", "unlock", "leverage", "next-generation", "state-of-the-art",
)
# Tools subsections that the maintainers curate by hand.
HAND_CURATED = ("oldies", "bleeding edge")
COMMENT_URL = re.compile(r"/draft\s+<?(https?://[^\s>]+)")
ARTICLES, BLOGS, TOOLS = "articles", "blogs / news", "tools"


# --------------------------------------------------------------------------
# README structure


@dataclass
class Section:
    heading: str
    start: int  # index of the heading line
    end: int  # index after the last line before the next heading

    @property
    def key(self) -> str:
        text = re.sub(r"<a [^>]*></a>", "", self.heading)
        return re.sub(r"^[^\w]+", "", text).strip().lower()


def sections(lines: list[str], level: str, within: Optional[Section] = None) -> list[Section]:
    """Headings of one level ('## ' or '### '), optionally only inside another section."""
    lo, hi = (within.start + 1, within.end) if within else (0, len(lines))
    heads = [i for i in range(lo, hi) if lines[i].startswith(level)]
    out = []
    for i in heads:
        stop = hi
        for j in range(i + 1, hi):
            if lines[j].startswith("#"):
                stop = j
                break
        out.append(Section(lines[i][len(level):].strip(), i, stop))
    return out


def top_section(lines: list[str], key: str) -> Optional[Section]:
    """A `## ` section by name. Its end runs to the next `## `, so its subsections stay inside it."""
    heads = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    for n, i in enumerate(heads):
        sec = Section(lines[i][3:].strip(), i, heads[n + 1] if n + 1 < len(heads) else len(lines))
        if sec.key == key:
            return sec
    return None


def tool_subsections(lines: list[str]) -> list[Section]:
    tools = top_section(lines, TOOLS)
    if tools is None:
        return []
    return [s for s in sections(lines, "### ", tools) if not any(w in s.key for w in HAND_CURATED)]


def entry_lines(lines: list[str], sec: Section, prefix: str) -> list[int]:
    return [i for i in range(sec.start + 1, sec.end) if lines[i].startswith(prefix)]


def sort_key(line: str) -> str:
    """The text entries are alphabetized by: the authors, the source, or the repository path."""
    match = re.match(r"\* \((.*?)(?:,? \*et al\.\*)?(?:, ?\d{4}[a-z]?)?\)", line) or re.match(r"\* \[`([^`]*)`\]", line)
    return (match.group(1) if match else line).casefold()


def insert_position(lines: list[str], sec: Section, prefix: str, key: str) -> Optional[int]:
    """Where an entry whose sort_key is `key` goes among the entries that start with `prefix`."""
    existing = entry_lines(lines, sec, prefix)
    if not existing:
        return None  # an empty section needs its first entry by hand
    for i in existing:
        if sort_key(lines[i]) > key:
            return i
    return existing[-1] + 1


# --------------------------------------------------------------------------
# The mechanical checks


def count_numbers(text: str) -> list[str]:
    return re.findall(r"\d[\d,.]*\d|\d", text)


def check_description(name: str, description: str, source_text: str) -> list[str]:
    """Rules for a tool's description: lowercase start, no final period, short, factual."""
    problems = []
    words = description.split()
    if not description or not description[0].islower():
        problems.append("The description must start with a lowercase letter, like the other tools.")
    if description.endswith("."):
        problems.append("The description must not end with a period, like the other tools.")
    if len(words) >= MAX_WORDS:
        problems.append(f"The description has {len(words)} words; keep it under {MAX_WORDS}.")
    repo = name.split("/")[-1]
    if len(repo) >= 4 and repo.casefold() in description.casefold():
        problems.append("The description repeats the tool's name.")
    lowered = description.casefold()
    for word in BANNED:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            problems.append(f"The description uses the marketing word '{word}'.")
    haystack = source_text.replace(",", "")
    for number in count_numbers(description):
        if number.replace(",", "") not in haystack:
            problems.append(f"The number '{number}' does not appear in the fetched source.")
    return problems


def author_label(names: list[str]) -> str:
    """Authors as the list writes them: Smith, Smith & Jones, Smith, Jones & Lee, Smith *et al.*"""
    surnames = [n.split()[-1] for n in names if n.split()]
    if len(surnames) >= 4:
        return f"{surnames[0]} *et al.*"
    if len(surnames) == 3:
        return f"{surnames[0]}, {surnames[1]} & {surnames[2]}"
    return " & ".join(surnames)


def compose(kind: str, url: str, title: str, label: str, year: str, description: str = "") -> str:
    if kind == TOOLS:
        return f"* [`{title}`]({url}): {description}"
    return f"* ({label}, {year}) [{title}]({url})"


# --------------------------------------------------------------------------
# Fetching the candidate


@dataclass
class Page:
    url: str
    title: str
    text: str
    year: str
    kind: str = BLOGS
    authors: list[str] = field(default_factory=list)
    stars: int = 0
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def clean_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query) if not k.lower().startswith("utm_")]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query), fragment=""))


def arxiv_authors(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entry = root.find(f"{ATOM}entry")
    if entry is None:
        return []
    return [" ".join((a.findtext(f"{ATOM}name") or "").split()) for a in entry.findall(f"{ATOM}author")]


def published_year(html: str) -> Optional[str]:
    for pattern in (
        r'(?is)<meta[^>]+(?:property|name)="(?:article:published_time|date|pubdate)"[^>]+content="(\d{4})',
        r'(?is)"datePublished"\s*:\s*"(\d{4})',
        r'(?is)<time[^>]+datetime="(\d{4})',
    ):
        found = re.search(pattern, html)
        if found:
            return found.group(1)
    return None


def load_page(url: str, token: str) -> Page:
    match = ARXIV_ID.search(url)
    if match:
        body = fetch(f"https://export.arxiv.org/api/query?id_list={match.group(1)}", timeout=60,
                     retry_on=(429, 503), pauses=(10, 30, 60))
        text = body.decode("utf-8", "replace") if body else ""
        cands = parse_arxiv(text)
        if not cands:
            return Page(url, "", "", "", ARTICLES, problems=["Could not read the paper from the arXiv API."])
        c = cands[0]
        return Page(c.url, c.title, f"{c.title}. {c.summary}", c.date[:4], ARTICLES, authors=arxiv_authors(text))

    gh = re.match(r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/?$", url)
    if gh:
        slug = f"{gh.group(1)}/{gh.group(2).removesuffix('.git')}"
        repo = github_get(f"/repos/{slug}", token)
        if not isinstance(repo, dict) or "full_name" not in repo:
            return Page(url, "", "", "", TOOLS, problems=["Could not read the repository from the GitHub API."])
        problems = []
        stars = int(repo.get("stargazers_count") or 0)
        if stars < MIN_STARS:
            problems.append(f"The repository has {stars} stars; tools need at least {MIN_STARS}.")
        if repo.get("archived"):
            problems.append("The repository is archived.")
        pushed = repo.get("pushed_at", "")
        if pushed and datetime.fromisoformat(pushed.replace("Z", "+00:00")) < datetime.now(timezone.utc) - timedelta(days=MAX_IDLE_DAYS):
            problems.append("The repository has had no pushes in the last 6 months.")
        readme = github_get(f"/repos/{slug}/readme", token)
        text = ""
        if isinstance(readme, dict) and readme.get("content"):
            text = base64.b64decode(readme["content"]).decode("utf-8", "replace")[:6000]
        return Page(
            repo["html_url"], repo["full_name"], f"{repo['full_name']}. {repo.get('description') or ''}\n{text}",
            str(datetime.now(timezone.utc).year), TOOLS, stars=stars, problems=problems,
        )

    body = fetch(url)
    if not body:
        return Page(url, "", "", "", BLOGS, problems=["The URL did not resolve."])
    html = body.decode("utf-8", "replace")
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    meta = re.search(r'(?is)<meta[^>]+name="description"[^>]+content="([^"]*)"', html)
    text = " ".join(filter(None, [title.group(1) if title else "", meta.group(1) if meta else "", strip_html(html)[:6000]]))
    year = published_year(html)
    page = Page(clean_url(url), " ".join((title.group(1) if title else "").split()), text,
                year or str(datetime.now(timezone.utc).year), BLOGS)
    if not year:
        page.notes.append("The page gives no publication date, so the year is today's. Check it.")
    return page


# --------------------------------------------------------------------------
# The model

SCOPE = (
    "You help maintain an awesome list about safety-critical AI: building, assuring, certifying, and operating "
    "AI and machine learning systems where failures can harm people, such as aviation, automotive, medical, rail, "
    "space, and energy. You will be given the text of one web page. Decide whether it belongs and, if so, "
    "fill in its entry.\n\n"
    "Set fits=false, with a one-sentence reason, if the page is marketing, a product announcement, funding news, "
    "filler, a listicle, padded writing with bizspeak, general AI ethics or hype with no engineering substance, or "
    "has no checkable artifact such as code, data, a method, or a source; is machine learning applied to a "
    "domain with no safety, assurance, or reliability angle; or only mentions safety in passing. When in doubt "
    "set fits=false. When fits is true, evidence must be an exact quote of 20 to 200 characters copied from the "
    "page text that shows it is about safety, assurance, or reliability of AI systems; when fits is false, use null."
)


def build_messages(page: Page, subsections: list[str]) -> list[dict]:
    if page.kind == TOOLS:
        extra = (
            "Pick exactly one subsection from this list, copied exactly:\n" + "\n".join(f"- {s}" for s in subsections) +
            "\n\nWrite a description of at most 25 words that starts with a lowercase letter and has no final "
            "period, like 'a library for ...'. No marketing words. Do not repeat the name. Use only numbers that "
            'appear in the page text.\n\nReply with JSON only: {"fits": bool, "reason": str, "subsection": str, '
            '"description": str}.'
        )
    elif page.kind == BLOGS:
        extra = (
            "Give the name of the publication or site that hosts the post, as short as it is usually written, "
            "and the post's title copied from the page.\n\n"
            'Reply with JSON only: {"fits": bool, "reason": str, "evidence": str|null, "source": str, "title": str}.'
        )
    else:
        extra = 'This is a paper. Reply with JSON only: {"fits": bool, "reason": str, "evidence": str|null}.'
    return [
        {"role": "system", "content": SCOPE + "\n\n" + extra},
        {"role": "user", "content": f"URL: {page.url}\nTitle: {page.title}\n\nPage text:\n{page.text[:6000]}"},
    ]


def ask_model(messages: list[dict], token: str, model: str) -> Optional[dict]:
    content = chat(messages, token, model)
    if content is None:
        return None
    answer = parse_answer(content)
    if answer is None:
        print(f"warning: no usable JSON in the model's answer: {content[:300]!r}", file=sys.stderr)
    return answer


def parse_answer(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("fits"), bool) else None


# --------------------------------------------------------------------------
# Putting it together


@dataclass
class Result:
    ok: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: Optional[list[str]] = None
    entry: str = ""
    title: str = ""
    slug: str = ""
    section: str = ""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "entry"


def draft(readme: str, page: Page, answer: dict, known: set[str]) -> Result:
    lines = readme.split("\n")
    result = Result(False)

    if page.problems:
        result.problems += page.problems
        return result
    if not answer.get("fits"):
        result.problems.append(f"The model declined: {answer.get('reason') or 'no reason given'}")
        return result
    if normalize(page.url) in known:
        result.problems.append("This URL is already in the README, an open PR, or an earlier scout issue.")
        return result

    evidence = " ".join(str(answer.get("evidence") or "").split())
    if len(evidence) < 20 or " ".join(evidence.casefold().split()) not in " ".join(page.text.casefold().split()):
        result.problems.append(
            "The model gave no quote from the page showing it is about safety-critical AI, or the quote is not on the page."
        )
        return result

    description = ""
    if page.kind == TOOLS:
        sec = next((s for s in tool_subsections(lines) if s.heading == str(answer.get("subsection"))), None)
        if sec is None:
            result.problems.append(f"'{answer.get('subsection')}' is not a Tools subsection this script can add to.")
            return result
        prefix, name, label = "* [`", page.title, ""
        description = str(answer.get("description") or "").strip()
        result.problems += check_description(name, description, page.text)
    else:
        sec = top_section(lines, page.kind)
        if sec is None:
            result.problems.append(f"The README has no '{page.kind}' section.")
            return result
        prefix = "* ("
        if page.kind == ARTICLES:
            name = page.title
            label = author_label(page.authors)
            if not label:
                result.problems.append("The arXiv record lists no authors.")
        else:
            name = " ".join(str(answer.get("title") or "").split())
            label = " ".join(str(answer.get("source") or "").split())
            if not name or name.casefold() not in page.text.casefold():
                result.problems.append(f"The title '{name}' does not appear in the fetched page.")
            if not label:
                result.problems.append("No publication name for the entry.")
            else:
                host = urllib.parse.urlparse(page.url).netloc.casefold().replace("www.", "")
                squashed = re.sub(r"\W", "", label.casefold())
                if label.casefold() not in page.text.casefold() and squashed not in re.sub(r"\W", "", host):
                    result.problems.append(f"The publication name '{label}' appears neither on the page nor in its address.")

    if result.problems:
        return result
    entry = compose(page.kind, page.url, name, label, page.year, description)
    pos = insert_position(lines, sec, prefix, sort_key(entry))
    if pos is None:
        result.problems.append("Could not find where to insert the entry.")
        return result
    new = lines[:pos] + [entry] + lines[pos:]
    return Result(True, [], list(page.notes), new, entry, f"Add {name}", slugify(name), sec.heading)


def pr_body(result: Result, page: Page, issue: str, answer: dict) -> str:
    checks = {
        TOOLS: f"the URL resolves and is not listed, the repository has at least {MIN_STARS} stars, is not archived and "
               "has been pushed to within six months, the description follows the tools style, every number appears "
               "in the fetched source",
        ARTICLES: "the URL resolves and is not listed, and the title and authors come from the arXiv record",
        BLOGS: "the URL resolves and is not listed, and the title and publication name appear on the page or its address",
    }[page.kind]
    out = [
        f"Drafted by `scripts/draft.py` from [{issue}] and **not yet checked by a person**. "
        "Open the source, read the entry against it, and check every claim before merging.",
        "",
        f"- Source: {page.url}",
        f"- Section: {result.section}",
        f"- Model's reason: {answer.get('reason') or 'none given'}",
        f"- Mechanical checks passed: {checks}, and the entry sits in alphabetical position.",
        "",
        "```md",
        result.entry,
        "```",
    ]
    if result.warnings:
        out += [""] + [f"> {w}" for w in result.warnings]
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--readme", default="README.md")
    parser.add_argument("--report", default="draft-report.md")
    parser.add_argument("--issue", default="a scout issue", help="how to refer to the source issue")
    parser.add_argument("--model", default=os.environ.get("SCOUT_MODEL") or DEFAULT_MODEL)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    comment = os.environ.get("COMMENT_BODY", "")
    issue_body = os.environ.get("ISSUE_BODY", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    def finish(status: str, report: str, result: Optional[Result] = None) -> int:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"status={status}\n")
                if result and result.ok:
                    f.write(f"branch=draft/{result.slug}\ntitle={result.title}\n")
        print(f"{status}: {report.splitlines()[0] if report else ''}")
        return 0

    match = COMMENT_URL.search(comment)
    if not match or not token:
        return finish("rejected", "Usage: comment `/draft <url>` where the URL is one of the links in this issue.\n")
    url = match.group(1).rstrip(".,;)")
    if normalize(url) not in known_keys(issue_body):
        return finish("rejected", f"{url} is not linked from this issue, so I will not draft it.\n")

    with open(args.readme, encoding="utf-8") as f:
        readme = f.read()
    known = known_keys(readme)
    if repo:
        pulls = github_get(f"/repos/{repo}/pulls?state=open&per_page=100", token)
        for pr in pulls if isinstance(pulls, list) else []:
            known |= known_keys(pr.get("body") or "")

    page = load_page(url, token)
    menu = [s.heading for s in tool_subsections(readme.split("\n"))]
    api_key = os.environ.get("SCOUT_API_KEY", "")
    if not api_key and not page.problems:
        return finish("rejected", "The SCOUT_API_KEY secret is not set, so there is no model to draft with.\n")
    answer = None if page.problems else ask_model(build_messages(page, menu), api_key, args.model)
    if answer is None and not page.problems:
        return finish("rejected", "The model did not give a usable answer, so nothing was drafted. Try again later.\n")

    result = draft(readme, page, answer or {}, known)
    if not result.ok:
        return finish("rejected", "Nothing was drafted:\n\n" + "\n".join(f"- {p}" for p in result.problems) + "\n")

    with open(args.readme, "w", encoding="utf-8") as f:
        f.write("\n".join(result.lines or []))
    return finish("ok", pr_body(result, page, args.issue, answer or {}), result)


if __name__ == "__main__":
    sys.exit(main())
