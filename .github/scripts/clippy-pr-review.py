#!/usr/bin/env python3
"""Post clippy diagnostics as PR review comments with suggestion blocks.

Reads ``clippy.json`` (``cargo clippy --message-format=json``) plus
``pr-number`` and ``workspace-root`` from an artifact directory, fetches the
PR's diff hunks via the GitHub API, filters clippy diagnostics down to lines
that are part of the diff, and posts them as a PR review with inline
comments. Diagnostics carrying a ``MachineApplicable`` suggestion include a
``suggestion`` block so the fix can be applied directly from the PR UI.

Prior review comments authored by ``*[bot]`` users that carry our marker are
deleted before posting, so the review doesn't accumulate duplicates across
pushes.
"""

import json
import os
import posixpath
import subprocess
import sys
from pathlib import Path

MARKER = "<!-- clippy-pr-review -->"
MAX_COMMENTS_PER_REVIEW = 50


def gh(args, *, input_data=None):
    return subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
        input=input_data,
    ).stdout


def gh_paginate(path):
    out = gh(["api", "--paginate", "--jq", ".[]", path])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def diff_lines_per_file(owner, repo, pr):
    """Map each PR file to the set of right-side line numbers a review may anchor to."""
    result = {}
    for f in gh_paginate(f"/repos/{owner}/{repo}/pulls/{pr}/files?per_page=100"):
        patch = f.get("patch")
        if not patch:
            continue
        lines = set()
        new_line = None
        for hunk_line in patch.splitlines():
            if hunk_line.startswith("@@"):
                try:
                    plus = hunk_line.split("+", 1)[1].split(" ", 1)[0]
                    new_line = int(plus.split(",")[0])
                except (IndexError, ValueError):
                    new_line = None
                continue
            if new_line is None:
                continue
            if hunk_line.startswith("+"):
                lines.add(new_line)
                new_line += 1
            elif not hunk_line.startswith("-"):
                new_line += 1
        result[f["filename"]] = lines
    return result


def iter_clippy_messages(path):
    with open(path) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if rec.get("reason") == "compiler-message":
                yield rec


def primary_span(spans):
    for s in spans:
        if s.get("is_primary"):
            return s
    return spans[0] if spans else None


def normalize_path(file_name, manifest_path, workspace_root):
    """Convert clippy's span path to one relative to the repo root, or None."""
    if not file_name:
        return None
    workspace_root = workspace_root.rstrip("/")
    if file_name.startswith("/"):
        abs_path = posixpath.normpath(file_name)
    elif manifest_path:
        crate_dir = manifest_path.rsplit("/", 1)[0] if "/" in manifest_path else ""
        abs_path = posixpath.normpath(f"{crate_dir}/{file_name}")
    else:
        rel = posixpath.normpath(file_name)
        return None if rel.startswith("..") else rel
    prefix = workspace_root + "/"
    if abs_path.startswith(prefix):
        return abs_path[len(prefix) :]
    return None


def build_suggestion_text(span):
    """Reconstruct the full-line replacement text for a ``suggestion`` block."""
    text = span.get("text") or []
    if not text:
        return None
    first, last = text[0], text[-1]
    try:
        prefix = first["text"][: first["highlight_start"] - 1]
        suffix = last["text"][last["highlight_end"] - 1 :]
    except (KeyError, TypeError):
        return None
    return prefix + (span.get("suggested_replacement") or "") + suffix


def build_comment(message, span, path):
    code = (message.get("code") or {}).get("code") or "clippy"
    text = (message.get("message") or "").strip()
    body = [MARKER, f"**[{code}]** {text}"]

    if (
        span.get("suggested_replacement") is not None
        and span.get("suggestion_applicability") == "MachineApplicable"
    ):
        suggestion = build_suggestion_text(span)
        if suggestion is not None:
            body += ["", "```suggestion", suggestion.rstrip("\n"), "```"]

    comment = {"path": path, "body": "\n".join(body), "side": "RIGHT"}
    line_start = span["line_start"]
    line_end = span["line_end"]
    if line_start == line_end:
        comment["line"] = line_end
    else:
        comment["start_line"] = line_start
        comment["start_side"] = "RIGHT"
        comment["line"] = line_end
    return comment


def cleanup_prior(owner, repo, pr):
    for c in gh_paginate(f"/repos/{owner}/{repo}/pulls/{pr}/comments?per_page=100"):
        user = (c.get("user") or {}).get("login") or ""
        if user.endswith("[bot]") and MARKER in (c.get("body") or ""):
            try:
                gh(["api", "--method", "DELETE", f"/repos/{owner}/{repo}/pulls/comments/{c['id']}"])
            except subprocess.CalledProcessError as e:
                print(f"failed to delete comment {c['id']}: {e.stderr}", file=sys.stderr)


def collect_comments(clippy_json, workspace_root, diff_lines):
    comments = []
    seen = set()
    for record in iter_clippy_messages(clippy_json):
        msg = record.get("message") or {}
        if msg.get("level") not in {"warning", "error"}:
            continue
        code = (msg.get("code") or {}).get("code") or ""
        if not code.startswith("clippy::"):
            continue
        span = primary_span(msg.get("spans") or [])
        if span is None:
            continue
        path = normalize_path(span.get("file_name", ""), record.get("manifest_path"), workspace_root)
        if not path:
            continue
        commentable = diff_lines.get(path)
        if not commentable:
            continue
        line_start = span.get("line_start")
        line_end = span.get("line_end")
        if not line_start or not line_end or line_end not in commentable:
            continue
        if line_start != line_end and line_start not in commentable:
            continue
        key = (path, line_start, line_end, code, msg.get("message"))
        if key in seen:
            continue
        seen.add(key)
        comments.append(build_comment(msg, span, path))
    return comments


def main():
    artifact = Path(sys.argv[1])
    pr = int((artifact / "pr-number").read_text().strip())
    workspace_root = (artifact / "workspace-root").read_text().strip()
    clippy_json = artifact / "clippy.json"

    owner, repo = os.environ["GITHUB_REPOSITORY"].split("/", 1)
    head_sha = os.environ["HEAD_SHA"]

    diff_lines = diff_lines_per_file(owner, repo, pr)
    comments = collect_comments(clippy_json, workspace_root, diff_lines) if diff_lines else []
    cleanup_prior(owner, repo, pr)

    if not comments:
        print("No clippy diagnostics on PR diff lines.")
        return

    for i in range(0, len(comments), MAX_COMMENTS_PER_REVIEW):
        chunk = comments[i : i + MAX_COMMENTS_PER_REVIEW]
        payload = {
            "commit_id": head_sha,
            "event": "COMMENT",
            "body": f"{MARKER}\n`cargo clippy` flagged {len(comments)} issue(s) on lines changed by this PR.",
            "comments": chunk,
        }
        gh(
            [
                "api",
                "--method", "POST",
                f"/repos/{owner}/{repo}/pulls/{pr}/reviews",
                "--input", "-",
            ],
            input_data=json.dumps(payload),
        )

    print(f"Posted {len(comments)} clippy comment(s) on PR #{pr}.")


if __name__ == "__main__":
    main()
