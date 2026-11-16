#!/usr/bin/env python3
"""Draft every candidate in a scout issue, one pull request each.

Run by the Draft workflow when a maintainer comments `/draft all`. For each
candidate link in the issue it runs scripts/draft.py exactly as `/draft <url>`
would, then commits the one-line change to its own branch and opens its own
pull request. Candidates that fail a check get no pull request; the report
lists the reason for each. Pauses between candidates keep the free model and
arXiv rate limits happy, and a cap keeps one comment from opening dozens of PRs.

    GITHUB_TOKEN=... GH_TOKEN=... SCOUT_API_KEY=... ISSUE_BODY='...' ISSUE_NUMBER=3 \\
        python3 scripts/draft_all.py --report draft-report.md

Writes `status=all` to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Optional

from scout import normalize

CAP = 10
PAUSE = 10
CANDIDATE = re.compile(r"^\s*[-*] \[[^\]]*\]\((https?://[^)\s]+)\)", re.M)


def candidate_urls(issue_body: str) -> list[str]:
    seen: set[str] = set()
    out = []
    for url in CANDIDATE.findall(issue_body):
        key = normalize(url)
        if key not in seen:
            seen.add(key)
            out.append(url)
    return out


def run(*cmd: str, env: Optional[dict] = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, check=check, capture_output=True, text=True)


def read_output(path: str) -> dict[str, str]:
    values = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                values[k] = v
    return values


def draft_one(url: str, issue_body: str, run_number: str, issue_ref: str) -> tuple[str, str]:
    """Returns (outcome, detail): 'pr' with the PR URL, or 'skipped' with the reason."""
    with tempfile.TemporaryDirectory() as tmp:
        out_path, report = os.path.join(tmp, "out"), os.path.join(tmp, "report.md")
        open(out_path, "w").close()
        env = {**os.environ, "COMMENT_BODY": f"/draft {url}", "ISSUE_BODY": issue_body, "GITHUB_OUTPUT": out_path}
        proc = run(sys.executable, "scripts/draft.py", "--report", report, "--issue", issue_ref, env=env, check=False)
        values = read_output(out_path)
        text = open(report, encoding="utf-8").read() if os.path.exists(report) else ""
        if proc.returncode != 0 or values.get("status") != "ok":
            reasons = [ln[2:] for ln in text.splitlines() if ln.startswith("- ")]
            return "skipped", "; ".join(reasons) or text.strip().splitlines()[0] if text.strip() else (proc.stderr.strip()[-200:] or "no result")
        branch, title = values["branch"], values["title"]
        if os.path.exists("scripts/align_tables.py"):
            run(sys.executable, "scripts/align_tables.py", "README.md", check=False)
        if run("git", "ls-remote", "--exit-code", "--heads", "origin", branch, check=False).returncode == 0:
            branch = f"{branch}-{run_number}"
        run("git", "switch", "-c", branch)
        run("git", "add", "README.md")
        run("git", "commit", "-m", title)
        run("git", "push", "-u", "origin", branch)
        pr = run("gh", "pr", "create", "--title", title, "--body-file", report, "--label", "scout",
                 "--base", "main", "--head", branch)
        return "pr", pr.stdout.strip()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", default="draft-report.md")
    args = parser.parse_args(argv)

    issue_body = os.environ.get("ISSUE_BODY", "")
    issue_ref = f"#{os.environ.get('ISSUE_NUMBER', '')}" if os.environ.get("ISSUE_NUMBER") else "a scout issue"
    run_number = os.environ.get("GITHUB_RUN_NUMBER", "0")
    urls = candidate_urls(issue_body)
    lines = []
    opened = 0
    for n, url in enumerate(urls[:CAP]):
        if n:
            time.sleep(PAUSE)
        try:
            outcome, detail = draft_one(url, issue_body, run_number, issue_ref)
        except subprocess.CalledProcessError as error:
            outcome, detail = "skipped", f"git or gh failed: {(error.stderr or '').strip()[-200:]}"
        finally:
            run("git", "checkout", "--", ".", check=False)
            run("git", "switch", "main", check=False)
        if outcome == "pr":
            opened += 1
            lines.append(f"- {url}: opened {detail}")
        else:
            lines.append(f"- {url}: no PR. {detail}")
        print(lines[-1])

    header = f"Tried {min(len(urls), CAP)} of {len(urls)} candidates and opened {opened} pull request(s). Please check each against its source before merging."
    if not urls:
        header = "No candidate links found in this issue."
    elif len(urls) > CAP:
        header += f" The cap is {CAP} per comment; comment `/draft all` again for the rest, or `/draft <url>` for one."
    with open(args.report, "w", encoding="utf-8") as f:
        f.write(header + "\n\n" + "\n".join(lines) + "\n")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("status=all\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
