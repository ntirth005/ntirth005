#!/usr/bin/env python3
"""Aggregate coding style distribution from repos and update README markers.

Expected per-repo file (default: .github/code-distribution.json):
{
    "manual_code": 70,
    "vibe_code": 30
}
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


README_PATH = Path("README.md")
MARKER_START = "<!-- CODE_STYLE_DISTRIBUTION_START -->"
MARKER_END = "<!-- CODE_STYLE_DISTRIBUTION_END -->"


@dataclass
class RepoDistribution:
    name: str
    manual: float
    vibe: float
    weight: float


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def github_request(url: str, token: str | None) -> Any:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "code-distribution-updater")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        body = resp.read().decode(charset)
        return json.loads(body)


def list_repositories(username: str, token: str | None) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "per_page": 100,
                "page": page,
                "type": "owner",
                "sort": "updated",
                "direction": "desc",
            }
        )
        url = f"https://api.github.com/users/{username}/repos?{query}"
        batch = github_request(url, token)
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_repo_distribution(owner: str, repo: str, path: str, token: str | None) -> tuple[float, float] | None:
    encoded_path = urllib.parse.quote(path, safe="")
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
    try:
        payload = github_request(url, token)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 404}:
            return None
        raise

    if payload.get("encoding") != "base64" or "content" not in payload:
        return None

    decoded = base64.b64decode(payload["content"]).decode("utf-8")
    data = json.loads(decoded)

    # Support legacy keys so existing repos keep working during migration.
    manual = float(
        data.get(
            "manual_code",
            data.get("hard_coded_pct", data.get("hard", -1)),
        )
    )
    vibe = float(
        data.get(
            "vibe_code",
            data.get(
                "ai_assisted_pct",
                data.get("vibe_coded_pct", data.get("vibe", -1)),
            ),
        )
    )

    if manual < 0 or vibe < 0:
        return None
    if manual > 100 or vibe > 100:
        return None
    if abs((manual + vibe) - 100.0) > 0.01:
        return None
    return manual, vibe


def render_block(rows: list[RepoDistribution], overall_manual: float, overall_vibe: float, username: str) -> str:
    chart_config = {
        "type": "doughnut",
        "data": {
            "labels": ["Manual Code", "Vibe Code"],
            "datasets": [
                {
                    "data": [round(overall_manual, 2), round(overall_vibe, 2)],
                    "backgroundColor": ["#2ea043", "#0969da"],
                    "borderColor": "#0d1117",
                    "borderWidth": 3,
                }
            ],
        },
        "options": {
            "plugins": {
                "legend": {
                    "position": "bottom",
                    "labels": {"color": "#c9d1d9", "boxWidth": 14},
                }
            },
            "cutout": "62%",
        },
    }
    chart_url = (
        "https://quickchart.io/chart?width=420&height=280&c="
        + urllib.parse.quote(json.dumps(chart_config, separators=(",", ":")))
    )

    lines: list[str] = []
    lines.append(MARKER_START)
    lines.append('<p align="center">')
    lines.append(
        f'  <img src="https://img.shields.io/badge/Manual_Code-{overall_manual:.1f}%25-2ea043?style=for-the-badge"/>'
    )
    lines.append(
        f'  <img src="https://img.shields.io/badge/Vibe_Code-{overall_vibe:.1f}%25-0969da?style=for-the-badge"/>'
    )
    lines.append("</p>")
    lines.append("")
    lines.append(f'<p align="center"><img src="{chart_url}" alt="Coding style distribution chart"/></p>')
    lines.append("")
    lines.append("| Repository | Manual Code | Vibe Code | Weight |")
    lines.append("|---|---:|---:|---:|")
    for row in rows:
        lines.append(
            f"| [{row.name}](https://github.com/{username}/{row.name}) | {row.manual:.1f}% | {row.vibe:.1f}% | {row.weight:.1f} |"
        )
    lines.append(MARKER_END)
    return "\n".join(lines)


def replace_block(readme: str, block: str) -> str:
    if MARKER_START not in readme or MARKER_END not in readme:
        raise RuntimeError(
            "README markers not found. Add CODE_STYLE_DISTRIBUTION_START/END markers first."
        )

    start = readme.index(MARKER_START)
    end = readme.index(MARKER_END) + len(MARKER_END)
    return readme[:start] + block + readme[end:]


def main() -> int:
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_TOKEN")
    distribution_file_path = os.getenv("DISTRIBUTION_FILE_PATH", ".github/code-distribution.json")
    include_forks = env_bool("INCLUDE_FORKS", False)

    if not username:
        print("GITHUB_USERNAME is required", file=sys.stderr)
        return 1

    if not README_PATH.exists():
        print("README.md not found", file=sys.stderr)
        return 1

    repos = list_repositories(username, token)
    rows: list[RepoDistribution] = []

    for repo in repos:
        if repo.get("archived"):
            continue
        if not include_forks and repo.get("fork"):
            continue

        name = repo["name"]
        weight = float(repo.get("size", 1) or 1)
        distribution = fetch_repo_distribution(username, name, distribution_file_path, token)
        if not distribution:
            continue

        manual, vibe = distribution
        rows.append(RepoDistribution(name=name, manual=manual, vibe=vibe, weight=weight))

    if not rows:
        print("No repositories with valid code-distribution metadata found", file=sys.stderr)
        return 1

    total_weight = sum(r.weight for r in rows)
    overall_manual = sum(r.manual * r.weight for r in rows) / total_weight
    overall_vibe = 100.0 - overall_manual

    rows.sort(key=lambda x: x.weight, reverse=True)

    readme = README_PATH.read_text(encoding="utf-8")
    block = render_block(rows, overall_manual, overall_vibe, username)
    updated = replace_block(readme, block)

    README_PATH.write_text(updated, encoding="utf-8")

    print(
        f"Updated README with {len(rows)} repos. "
        f"Overall: manual={overall_manual:.1f}% vibe={overall_vibe:.1f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
