"""Post cumulative automated grades to a PR conversation.

This script runs ONLY in a trusted workflow_run workflow on the default branch.
It reads the untrusted test artifact as data and NEVER executes student code.
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ASSIGNMENTS = [
    ("adding_numbers.py", "Adding Numbers"),
    ("list_of_multiples.py", "List of Multiples"),
    ("fizz_buzz.py", "FizzBuzz"),
    ("basic_calculator.py", "Basic Calculator"),
    ("country_area.py", "Get the Area of a Country"),
    ("find_the_discount.py", "Find the Discount"),
    ("calc_the_profit.py", "Calculate the Profit"),
    ("circle_or_square.py", "Circle or Square"),
    ("how_many_vowels.py", "How Many Vowels?"),
    ("calc_damage.py", "Calculating Damage"),
]
VALID = dict(ASSIGNMENTS)
# Hidden marker stores just the verified score information for THIS PR.
# Other PRs' bot comments are our persistent per-student grading history.
MARKER = "<!-- python-autograder-record:v1 "
RECORD_RE = re.compile(r"<!-- python-autograder-record:v1 ([A-Za-z0-9_+/=.-]+) -->")
BOT_USER = "github-actions[bot]"
MAX_PAGES = 20


def api(method, endpoint, payload=None):
    repo = os.environ["GITHUB_REPOSITORY"]
    url = "https://api.github.com/repos/" + repo + endpoint
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "school-python-autograder",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response) if response.status != 204 else None
    except urllib.error.HTTPError as error:
        print(f"GitHub {method} {endpoint}: HTTP {error.code}", file=sys.stderr)
        raise


def all_pages(endpoint):
    records = []
    for page in range(1, MAX_PAGES + 1):
        separator = "&" if "?" in endpoint else "?"
        data = api("GET", endpoint + f"{separator}per_page=100&page={page}")
        records.extend(data)
        if len(data) < 100:
            return records
    raise RuntimeError("Too many pages of results; refusing incomplete grade history")


def read_record(body, author):
    match = RECORD_RE.search(body or "")
    if not match:
        return None
    try:
        # Record is base64-encoded JSON to prevent Markdown formatting collisions.
        import base64
        decoded = base64.urlsafe_b64decode(match.group(1).encode("ascii"))
        record = json.loads(decoded)
        if record.get("version") != 1 or record.get("author") != author:
            return None
        if not isinstance(record.get("run_id"), int) or record["run_id"] < 1:
            return None
        if not isinstance(record.get("run_attempt"), int) or record["run_attempt"] < 1:
            return None
        if not isinstance(record.get("head_sha"), str) or not re.fullmatch(r"[a-f0-9]{40}", record["head_sha"]):
            return None
        scores = validate_scores(record["scores"])
        return {**record, "scores": scores}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def validate_scores(scores):
    if not isinstance(scores, dict) or not 1 <= len(scores) <= len(VALID):
        raise ValueError("Invalid problem score map")
    clean = {}
    for filename, value in scores.items():
        if filename not in VALID or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Unknown filename or invalid score")
        if not math.isfinite(value) or not 0 <= value <= 10:
            raise ValueError("Invalid score range")
        clean[filename] = round(float(value), 6)
    return clean


def encode_record(record):
    import base64
    encoded = base64.urlsafe_b64encode(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return MARKER + encoded + " -->"


def comments_for_pr(pr_number):
    return all_pages(f"/issues/{pr_number}/comments")


def find_bot_record(comments, author):
    found = []
    for comment in comments:
        if comment.get("user", {}).get("login") != BOT_USER:
            continue
        record = read_record(comment.get("body"), author)
        if record:
            found.append((comment["id"], record))
    return max(found, key=lambda pair: (pair[1]["run_id"], pair[1]["run_attempt"]), default=(None, None))


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    run = event["workflow_run"]
    if (run.get("event") != "pull_request" or
            run.get("name") != "Grade Python Pull Request"):
        raise SystemExit("Not an expected PR grading run")
    if run.get("head_repository", {}).get("full_name") is None:
        raise SystemExit("Run does not identify its head repository")

    artifact = Path("grade-download/grade.json")
    details_file = Path("grade-download/details.md")
    if not artifact.is_file() or not details_file.is_file():
        raise SystemExit("No complete grading artifact; nothing to publish")
    if artifact.stat().st_size > 12000 or details_file.stat().st_size > 40000:
        raise SystemExit("Grading artifact exceeds size limits")
    incoming = json.loads(artifact.read_text(encoding="utf-8"))
    if incoming.get("version") != 1 or not isinstance(incoming.get("pr_number"), int):
        raise SystemExit("Invalid grading artifact version or PR number")
    number = incoming["pr_number"]
    if number <= 0:
        raise SystemExit("Invalid PR number")
    sha = incoming.get("head_sha", "")
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise SystemExit("Invalid head SHA")
    scores = validate_scores(incoming.get("scores"))

    pr = api("GET", f"/pulls/{number}")
    author = pr["user"]["login"]
    if (pr.get("head", {}).get("sha") != sha or
            run.get("head_sha") != sha or
            pr.get("base", {}).get("ref") != "main"):
        raise SystemExit("Run is stale or doesn't match the submitted PR; not updating grades")
    if run.get("head_repository", {}).get("full_name") != pr.get("head", {}).get("repo", {}).get("full_name"):
        raise SystemExit("Head repository mismatch")

    # Gather the latest graded record for each of the student's PRs.
    # Records posted by students or other bots never enter the grade history.
    # We don't run any code or use source files from these PRs.
    prs = all_pages("/pulls?state=all")
    latest_by_problem = {}
    current_comment_id = None
    newest_for_this_pr = None
    for other in prs:
        if other.get("user", {}).get("login") != author:
            continue
        other_number = other["number"]
        comments = comments_for_pr(other_number)
        comment_id, record = find_bot_record(comments, author)
        if other_number == number:
            current_comment_id = comment_id
            newest_for_this_pr = record
        if not record or other_number == number:
            continue
        for filename, score in record["scores"].items():
            order = (record["run_id"], record["run_attempt"])
            if filename not in latest_by_problem or order > latest_by_problem[filename][0]:
                latest_by_problem[filename] = (order, score)

    run_id = int(run["id"])
    attempt = int(run.get("run_attempt", 1))
    if newest_for_this_pr and (newest_for_this_pr["run_id"], newest_for_this_pr["run_attempt"]) > (run_id, attempt):
        raise SystemExit("Newer grading record already published; not replacing it")
    this_order = (run_id, attempt)
    for filename, score in scores.items():
        if filename not in latest_by_problem or this_order >= latest_by_problem[filename][0]:
            latest_by_problem[filename] = (this_order, score)

    total = sum(latest_by_problem.get(filename, ((), 0))[1] for filename in VALID)
    current_score = sum(scores.values())
    rows = [
        "## Python Assignment — Grading Progress", "",
        f"**Current submission: {current_score:.2f}/{10*len(scores)} points**",
        f"**Overall grade: {total:.2f}/100 points**",
        "",
        "| Problem | Status | Score |",
        "|---|---|---:|",
    ]
    for filename, title in ASSIGNMENTS:
        if filename in scores:
            status = "Graded in this PR"
        elif filename in latest_by_problem:
            status = "Graded previously"
        else:
            status = "Not yet graded"
        score = latest_by_problem.get(filename, ((), 0))[1]
        rows.append(f"| {title} | {status} | {score:.2f}/10 |")
    rows += ["", f"**TOTAL: {total:.2f}/100**", "", "---", "",
             "<details><summary>Show individual test results and debugging feedback</summary>",
             "",
             details_file.read_text(encoding="utf-8")[:30000],
             "", "</details>", "",
             "Scores update when a new grading run finishes. Previously ungraded problems receive 0/10.",
             "The latest graded result for each problem is used; instructor review may change the final grade.",
             ""]

    record = {
        "version": 1,
        "author": author,
        "run_id": run_id,
        "run_attempt": attempt,
        "head_sha": sha,
        "scores": scores,
    }
    rows.append(encode_record(record))
    body = "\n".join(rows)
    # Keep the comment under GitHub's body limit.
    if len(body) > 60000:
        raise SystemExit("Grade comment unexpectedly large")

    if current_comment_id:
        api("PATCH", f"/issues/comments/{current_comment_id}", {"body": body})
        print(f"Updated grading feedback in PR #{number}")
    else:
        api("POST", f"/issues/{number}/comments", {"body": body})
        print(f"Posted grading feedback in PR #{number}")


if __name__ == "__main__":
    main()
