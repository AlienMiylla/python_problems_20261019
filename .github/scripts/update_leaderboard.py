"""Regenerate public Markdown leaderboard from trusted autograder PR comments.

Runs only in the privileged, serial workflow_run job after grading feedback is posted.
The output contains GitHub usernames as plain text, never profile hyperlinks.
"""
import base64
from datetime import datetime, timezone
from urllib.error import HTTPError

from publish_grades import ASSIGNMENTS, BOT_USER, all_pages, api, read_record

FILENAME = "LEADERBOARD.md"
TEST_PREFIX = "DO NOT MERGE"


def collect_ranking():
    """Latest graded score for each problem across each user's non-test PRs."""
    prs = all_pages("/pulls?state=all")
    pr_index = {
        pr["number"]: pr
        for pr in prs
        if not pr.get("title", "").strip().upper().startswith(TEST_PREFIX)
    }

    # Fetch bot comments once for the entire repository, instead of one
    # request per PR. This scales well as more students submit assignments.
    comments = all_pages("/issues/comments")
    latest_pr_record = {}
    for comment in comments:
        if comment.get("user", {}).get("login") != BOT_USER:
            continue
        issue_url = comment.get("issue_url", "")
        try:
            number = int(issue_url.rstrip("/").split("/")[-1])
        except (ValueError, TypeError):
            continue
        pr = pr_index.get(number)
        if not pr:
            continue
        author = pr.get("user", {}).get("login", "")
        if not author:
            continue
        record = read_record(comment.get("body", ""), author)
        if not record:
            continue
        ordering = (record["run_id"], record["run_attempt"])
        previous = latest_pr_record.get(number)
        if previous is None or ordering > previous[0]:
            latest_pr_record[number] = (ordering, author, record["scores"])

    latest_per_user = {}
    for order, author, scores in latest_pr_record.values():
        results = latest_per_user.setdefault(author, {})
        for filename, score in scores.items():
            previous = results.get(filename)
            if previous is None or order > previous[0]:
                results[filename] = (order, score)

    rows = []
    for username, problems in latest_per_user.items():
        scores = {filename: entry[1] for filename, entry in problems.items()}
        total = round(sum(scores.values()), 2)
        rows.append({
            "username": username,
            "points": total,
            "solved": sum(score >= 9.999999 for score in scores.values()),
            "attempted": len(scores),
        })

    # All scores are out of 100. Same displayed total receives the same rank.
    rows.sort(key=lambda row: (-row["points"], -row["solved"], row["username"].lower()))
    return rows


def markdown_for(rows):
    updated = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    lines = [
        "# Python Coding Challenge Leaderboard",
        "",
        "**Week of October 19, 2026**",
        "",
        f"*Last updated: {updated}*",
        "",
        "The leaderboard updates as pull requests are automatically graded.",
        "Each problem is worth 10 points, for a possible total of **100 points**.",
        "Unattempted problems contribute 0 points. The latest graded score for",
        "each problem is used, even across multiple pull requests.",
        "",
        "| Rank | GitHub username | Solved | Attempted | Total points |",
        "|---:|---|---:|---:|---:|",
    ]
    previous_points = None
    rank = 0
    for index, row in enumerate(rows, 1):
        if row["points"] != previous_points:
            rank = index
            previous_points = row["points"]
        username = row["username"]
        # GitHub usernames are limited to letters, numbers and hyphens.
        # Keep them as plain code text so they are not linked.
        lines.append(
            f"| {rank} | " + chr(96) + username + chr(96) +
            f" | {row['solved']}/10 | {row['attempted']}/10 | {row['points']:.2f}/100 |"
        )
    if not rows:
        lines.append("| — | No graded student submissions yet | — | — | — |")
    lines += [
        "",
        "**Solved** = earned all 10 points for that problem.",
        "**Attempted** = a problem with at least one recorded grade.",
        "",
        "Test pull requests titled DO NOT MERGE are excluded.",
        "Usernames are displayed without links.",
        "This is an informal coding-challenge leaderboard, not an official gradebook.",
        "",
    ]
    return "\n".join(lines)


def save_leaderboard(markdown):
    endpoint = f"/contents/{FILENAME}"
    existing_sha = None
    try:
        current = api("GET", endpoint + "?ref=main")
        existing_sha = current["sha"]
        old = base64.b64decode(current["content"]).decode("utf-8")
        if old == markdown:
            print("Leaderboard already matches scores. No commit needed.")
            return
    except HTTPError as exc:
        if exc.code != 404:
            raise

    payload = {
        "message": "Update Python challenge leaderboard",
        "content": base64.b64encode(markdown.encode("utf-8")).decode("ascii"),
        "branch": "main",
    }
    if existing_sha is not None:
        payload["sha"] = existing_sha

    response = api("PUT", endpoint, payload)
    print("Updated leaderboard:", response["content"]["html_url"])


def main():
    rows = collect_ranking()
    print(f"Updating leaderboard for {len(rows)} graded GitHub usernames.")
    save_leaderboard(markdown_for(rows))


if __name__ == "__main__":
    main()
