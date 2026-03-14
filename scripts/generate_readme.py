#!/usr/bin/env python3
"""Generate a Theo Browne-style GitHub profile README."""

import json
import os
import sys
from pathlib import Path

import requests
import tomlkit
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
TOML_PATH = ROOT / "projects.toml"
CACHE_PATH = ROOT / ".cache" / "descriptions.json"
README_PATH = ROOT / "README.md"

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

HEADERS = {"Accept": "application/vnd.github.v3+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"

INTRO = (
    "Full-stack developer from the UK. I build tools for home automation, "
    "self-hosting, and whatever else catches my attention."
)


def load_toml():
    with open(TOML_PATH) as f:
        return tomlkit.load(f)


def save_toml(config):
    with open(TOML_PATH, "w") as f:
        tomlkit.dump(config, f)


def load_cache():
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def github_get(url):
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_all_user_repos(username):
    """Fetch all public repos for a user, returns dict keyed by full_name."""
    page = 1
    repo_map = {}
    while True:
        repos = github_get(
            f"{GITHUB_API}/users/{username}/repos?per_page=100&page={page}"
        )
        if not repos:
            break
        for r in repos:
            repo_map[r["full_name"]] = r
        if len(repos) < 100:
            break
        page += 1
    return repo_map


def get_repo_info(repo_slug):
    """Fetch a single repo's metadata (for repos not in the user's list)."""
    try:
        return github_get(f"{GITHUB_API}/repos/{repo_slug}")
    except requests.HTTPError:
        return None


def format_stars(count):
    if count == 0:
        return ""
    if count >= 1000:
        return f" ({count / 1000:.1f}k stars)"
    return f" ({count} {'star' if count == 1 else 'stars'})"


def discover_repos(config, repo_map):
    """Find new repos not already listed and check worthiness."""
    ignore = set(config["discovery"].get("ignore", []))

    known_repos = set()
    for section in ("current_projects", "everything_else"):
        for project in config.get(section, []):
            if "repo" in project:
                known_repos.add(project["repo"])

    new_worthy = []
    for full_name, repo in repo_map.items():
        name = repo["name"]
        if repo["private"] or repo["fork"] or repo["archived"]:
            continue
        if name in ignore or full_name in known_repos:
            continue
        if not is_worthy(repo):
            continue
        new_worthy.append(full_name)

    return new_worthy


def is_worthy(repo):
    """A repo is worthy if it has substance worth showing."""
    has_homepage = bool(repo.get("homepage"))
    has_topics = len(repo.get("topics", [])) > 0
    size = repo.get("size", 0)

    if size < 10:
        return False
    if has_homepage or has_topics or repo.get("stargazers_count", 0) > 0:
        return True
    if repo.get("description") and size > 50:
        return True
    return False


def generate_descriptions(projects_needing_desc, cache):
    """Call OpenAI to generate descriptions for projects without one."""
    if not projects_needing_desc:
        return cache

    client = OpenAI(api_key=OPENAI_API_KEY)

    repo_lines = []
    for slug, repo_data in projects_needing_desc.items():
        desc = repo_data.get("description") or "No description"
        lang = repo_data.get("language") or "Unknown"
        topics = ", ".join(repo_data.get("topics", []))
        repo_lines.append(
            f"- {slug}: {desc} (language: {lang}, topics: {topics})"
        )

    prompt = (
        "Generate a one-sentence description for each GitHub repo below. "
        "Style: concise, slightly informal, confident. Focus on what it does "
        "for the user, not implementation details. Witty when it fits, don't "
        "force it. No corporate speak. Examples of good descriptions: "
        '"File uploads for modern web devs", '
        '"Redirect Bing somewhere better".\n\n'
        "Repos:\n" + "\n".join(repo_lines) + "\n\n"
        "Reply with JSON only: {\"repo/slug\": \"description\", ...}"
    )

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.7,
    )

    try:
        descriptions = json.loads(resp.choices[0].message.content)
    except (json.JSONDecodeError, IndexError):
        print("Warning: failed to parse OpenAI response", file=sys.stderr)
        return cache

    for slug, desc in descriptions.items():
        cache[slug] = desc

    return cache


def render_project_line(project, repo_info, cache):
    """Render a single project line."""
    slug = project.get("repo", "")
    fallback_name = slug.split("/")[-1] if slug else "Unknown"
    name = project.get("name") or (repo_info or {}).get("name", fallback_name)

    fallback_url = f"https://github.com/{slug}" if slug else ""
    url = project.get("url") or (repo_info or {}).get("html_url", fallback_url)

    desc = project.get("description")
    if not desc and slug in cache:
        desc = cache[slug]
    if not desc and repo_info:
        desc = repo_info.get("description") or ""
    if not desc:
        desc = ""

    stars = (repo_info or {}).get("stargazers_count", 0)
    star_str = format_stars(stars)

    if url:
        return f"- **[{name}]({url})** — {desc}{star_str}"
    return f"- **{name}** — {desc}{star_str}"


def main():
    config = load_toml()
    cache = load_cache()
    username = config["discovery"]["username"]

    # --- Fetch all repos in one batch ---
    print("Fetching repos from GitHub...")
    repo_map = fetch_all_user_repos(username)
    print(f"Found {len(repo_map)} public repos.")

    # --- Discovery ---
    print("Checking for new worthy repos...")
    new_repos = discover_repos(config, repo_map)
    if new_repos:
        print(f"Discovered {len(new_repos)} new repos: {new_repos}")
        for slug in new_repos:
            entry = tomlkit.table()
            entry.add("repo", slug)
            config["everything_else"].append(entry)
        save_toml(config)

    # --- Gather repo info and figure out what needs descriptions ---
    all_projects = []
    projects_needing_desc = {}

    for section in ("current_projects", "everything_else"):
        for project in config.get(section, []):
            slug = project.get("repo")

            # Use batch-fetched data if available, otherwise fetch individually
            repo_info = None
            if slug:
                repo_info = repo_map.get(slug)
                if repo_info is None and "/" in slug:
                    # Might be an org repo not in user's list
                    repo_info = get_repo_info(slug)

            all_projects.append((section, dict(project), repo_info))

            if not project.get("description") and slug and slug not in cache:
                if repo_info:
                    projects_needing_desc[slug] = repo_info

    # --- Generate descriptions ---
    if projects_needing_desc:
        print(f"Generating descriptions for {len(projects_needing_desc)} repos...")
        cache = generate_descriptions(projects_needing_desc, cache)
        save_cache(cache)
    else:
        print("All descriptions cached.")

    # --- Render README ---
    lines = [
        '<img src="profile_header.gif" width="100%" />\n',
        INTRO + "\n",
        "Check out my [website](https://davidilie.com)."
        " Support me on [ZeroCut](https://zerocut.gg/david).\n",
    ]

    for section, heading in [
        ("current_projects", "Current Projects"),
        ("everything_else", "Everything Else"),
    ]:
        lines.append(f"## {heading}\n")
        for sec, project, repo_info in all_projects:
            if sec != section:
                continue
            lines.append(render_project_line(project, repo_info, cache))
        lines.append("")

    readme_content = "\n".join(lines).rstrip() + "\n"

    with open(README_PATH, "w") as f:
        f.write(readme_content)

    print(f"README.md written ({len(readme_content)} bytes)")
    save_cache(cache)


if __name__ == "__main__":
    main()
