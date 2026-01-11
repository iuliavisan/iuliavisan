#!/usr/bin/env python3
import os
import math
import requests
import sys

# -------------------- CONFIG --------------------
TOP_N = 5
EXCLUDED_LANGUAGES = set()  # keep empty by default

# Visuals
BG_COLOR = "#0b0f1a"
TEXT_COLOR = "#e5e7eb"
MUTED_TEXT = "#9ca3af"
OTHER_COLOR = "#6b7280"

# Left / repo pie (warm, vibrant)
REPO_COLORS = ["#f97316", "#eab308", "#22c55e", "#fb7185", "#a78bfa"]

# Right / activity pie (high-contrast)
ACTIVITY_COLORS = ["#06b6d4", "#6366f1", "#00c2a8", "#ff6b6b", "#ffd166"]

OUTPUT_FILE = "languages-overview.svg"
# ------------------------------------------------

TOKEN = os.environ.get("GITHUB_TOKEN")
if not TOKEN:
    print("Error: GITHUB_TOKEN not set in environment.", file=sys.stderr)
    sys.exit(1)

GITHUB_API = "https://api.github.com/graphql"
USERNAME = os.environ.get("GH_USERNAME", os.environ.get("GITHUB_REPOSITORY", "").split("/")[0])
if not USERNAME:
    print("Error: Could not determine GH username. Set GH_USERNAME or run in a repo context.", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

# GraphQL: include primaryLanguage
QUERY = """
query ($login: String!, $after: String) {
  user(login: $login) {
    repositories(first: 100, after: $after, privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name
        primaryLanguage { name }
        languages(first: 20) { edges { node { name } } }
      }
    }
  }
}
"""

# -------------------- DATA FETCH --------------------

def fetch_repositories():
    repos = []
    cursor = None
    while True:
        resp = requests.post(GITHUB_API, headers=HEADERS, json={
            "query": QUERY,
            "variables": {"login": USERNAME, "after": cursor}
        })
        if resp.status_code != 200:
            print("GraphQL request failed:", resp.status_code, resp.text, file=sys.stderr)
            sys.exit(1)
        data = resp.json()
        if data.get("errors"):
            print("GraphQL errors:", data["errors"], file=sys.stderr)
            sys.exit(1)
        page = data["data"]["user"]["repositories"]
        repos.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return repos

def fetch_commit_count(repo_name):
    url = f"https://api.github.com/repos/{USERNAME}/{repo_name}/commits?per_page=1"
    r = requests.get(url, headers={"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        # non-fatal: return 1 as fallback, but print for debugging
        print(f"Warning: commit fetch for {repo_name} returned {r.status_code}; defaulting to 1", file=sys.stderr)
        return 1
    if "Link" not in r.headers:
        # small repos usually return full list
        try:
            return len(r.json())
        except Exception:
            return 1
    # parse last page number from Link header
    for part in r.headers["Link"].split(","):
        if 'rel="last"' in part:
            try:
                return int(part.split("page=")[-1].split(">")[0])
            except Exception:
                return 1
    return 1

# -------------------- AGGREGATION (use primaryLanguage) --------------------

def language_for_repo(repo):
    # Prefer primaryLanguage if available; else fallback to first language in languages list; else None
    pl = repo.get("primaryLanguage")
    if pl and pl.get("name"):
        name = pl["name"]
    else:
        edges = repo.get("languages", {}).get("edges", [])
        name = edges[0]["node"]["name"] if edges else None
    return name

def languages_by_repo_count(repos):
    counts = {}
    for r in repos:
        lang = language_for_repo(r)
        if not lang:
            continue
        if lang in EXCLUDED_LANGUAGES:
            continue
        counts[lang] = counts.get(lang, 0) + 1
    return counts

def commit_weighted_languages(repos):
    # Assign repo's commit count to its primary language (fallback if missing)
    weighted = {}
    for r in repos:
        lang = language_for_repo(r)
        if not lang or lang in EXCLUDED_LANGUAGES:
            continue
        commits = fetch_commit_count(r["name"])
        weighted[lang] = weighted.get(lang, 0) + commits
    return weighted

def top_n_with_other(data):
    items = sorted(data.items(), key=lambda x: x[1], reverse=True)
    top = items[:TOP_N]
    other = sum(v for _, v in items[TOP_N:])
    if other > 0:
        top.append(("Other", other))
    return top

# -------------------- SVG HELPERS --------------------

def pie_paths(data, cx, cy, r_outer=88, r_inner=56, colors=None):
    total = sum(v for _, v in data) or 1
    angle = -math.pi / 2
    result = []
    for i, (label, value) in enumerate(data):
        frac = value / total
        delta = frac * 2 * math.pi
        a1, a2 = angle, angle + delta
        large = 1 if delta > math.pi else 0
        color = OTHER_COLOR if label == "Other" else (colors[i % len(colors)])
        def pt(r, a): return cx + r * math.cos(a), cy + r * math.sin(a)
        x1, y1 = pt(r_outer, a1); x2, y2 = pt(r_outer, a2)
        x3, y3 = pt(r_inner, a2); x4, y4 = pt(r_inner, a1)
        d = f"M{x1},{y1} A{r_outer},{r_outer} 0 {large} 1 {x2},{y2} L{x3},{y3} A{r_inner},{r_inner} 0 {large} 0 {x4},{y4} Z"
        result.append((d, color, label, round(frac * 100)))
        angle = a2
    return result

def legend_svg(x, y, items):
    # items: list of (d, color, label, pct) as returned from pie_paths
    out = ""
    for i, (_, color, label, pct) in enumerate(items):
        yy = y + i*20
        out += f'<rect x="{x}" y="{yy-12}" width="12" height="12" fill="{color}" rx="2"/>\n'
        out += f'<text x="{x+18}" y="{yy-2}" font-size="12" fill="{TEXT_COLOR}">{label} — {pct}%</text>\n'
    return out

# -------------------- RENDER COMBINED --------------------

def render_combined(repo_data, activity_data):
    # compute top lists
    repo_top = top_n_with_other(repo_data)
    act_top = top_n_with_other(activity_data)

    # pies positions (increased gap)
    left_legend_x, left_legend_y = 40, 70
    left_pie_cx, left_pie_cy = 280, 160

    right_legend_x, right_legend_y = 440, 70
    right_pie_cx, right_pie_cy = 680, 160

    repo_paths = pie_paths(repo_top, left_pie_cx, left_pie_cy, colors=REPO_COLORS)
    act_paths  = pie_paths(act_top, right_pie_cx, right_pie_cy, colors=ACTIVITY_COLORS)

    svg = f'''<svg width="800" height="280" viewBox="0 0 800 280" xmlns="http://www.w3.org/2000/svg">
    <rect width="100%" height="100%" fill="{BG_COLOR}"/>
    <text x="40" y="36" font-size="16" fill="{TEXT_COLOR}">Languages by repositories</text>
    <text x="440" y="36" font-size="16" fill="{TEXT_COLOR}">Languages by activity</text>

    <!-- left legend -->
    {legend_svg(left_legend_x, left_legend_y, repo_paths)}
    <!-- right legend -->
    {legend_svg(right_legend_x, right_legend_y, act_paths)}
    # left pie
    {''.join(f'<path d="{d}" fill="{c}"/>' for d, c, _, _ in repo_paths)}
    # right pie
    {''.join(f'<path d="{d}" fill="{c}"/>' for d, c, _, _ in act_paths)}

    </svg>'''
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(svg)

# -------------------- MAIN --------------------

def main():
    repos = fetch_repositories()
    repo_data = languages_by_repo_count(repos)
    activity_data = commit_weighted_languages(repos)
    if not repo_data:
        print("No repository language data found.", file=sys.stderr)
    render_combined(repo_data, activity_data)
    print("Wrote", OUTPUT_FILE)

if __name__ == "__main__":
    main()
