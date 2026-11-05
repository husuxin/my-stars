#!/usr/bin/env python3
"""Turn a GitHub user's stars into a categorized README + searchable page.

Only public stars are read (GET /users/{login}/starred). The workflow's
GITHUB_TOKEN is used to raise the API rate limit and to push the result.
No third-party dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
USER_AGENT = "star-index-builder/1.0"
PER_PAGE = 100
BEIJING = timezone(timedelta(hours=8))

README_NAMES = (
    "README.md",
    "readme.md",
    "Readme.md",
    "README.MD",
    "README.rst",
    "README",
    "README.txt",
    "README.markdown",
    "README.adoc",
    "README.org",
)
README_MAX_BYTES = 200_000
README_EXCERPT = 700
README_CACHE_VERSION = 2


# ---------------------------------------------------------------- utilities


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def collapse(text) -> str:
    return " ".join((text or "").split())


def trim(text, limit: int) -> str:
    text = collapse(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:，。；：、") + "…"


def fmt_stars(count: int) -> str:
    count = int(count or 0)
    if count < 1000:
        return str(count)
    if count >= 1_000_000:
        value = count / 1_000_000
        return f"{value:.1f}M".replace(".0M", "M")
    value = count / 1000
    if value < 10:
        return f"{value:.1f}k".replace(".0k", "k")
    return f"{round(value)}k"


def fmt_date(iso: str) -> str:
    if not iso:
        return ""
    return iso[:10]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


def gh_link(login: str) -> str:
    return f"https://github.com/{login}?tab=stars"


def detect_login() -> str:
    for key in ("GH_LOGIN", "GITHUB_REPOSITORY_OWNER"):
        if os.environ.get(key):
            return os.environ[key]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        return repo.split("/", 1)[0]
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=Path(__file__).resolve().parents[1],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return ""
    match = re.search(r"github\.com[:/]([^/]+)/", url)
    return match.group(1) if match else ""


# ------------------------------------------------------------------- GitHub


def request_json(url: str, token: str | None):
    headers = {
        "Accept": "application/vnd.github.star+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response), response.headers.get("Link", "")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:300]
        if error.code == 404:
            raise SystemExit(
                f"GitHub 用户不存在或没有公开 star：{url}\n{detail}"
            ) from None
        if error.code in (403, 429):
            reset = error.headers.get("X-RateLimit-Reset")
            hint = ""
            if reset and reset.isdigit():
                wait = int(reset) - int(time.time())
                if 0 < wait <= 90 and error.headers.get("X-RateLimit-Remaining") == "0":
                    print(f"触发限流，等待 {wait}s 后重试…", file=sys.stderr)
                    time.sleep(wait + 2)
                    return request_json(url, token)
                hint = f"（配额重置约 {max(wait, 0) // 60} 分钟后）"
            raise SystemExit(
                "GitHub API 拒绝请求" + hint + "。请设置 GITHUB_TOKEN 或稍后重试。\n" + detail
            ) from None
        raise SystemExit(f"GitHub API 错误 {error.code}：{detail}") from None
    except urllib.error.URLError as error:
        raise SystemExit(f"无法连接 GitHub API：{error.reason}") from None


def fetch_all(login: str, token: str | None):
    items = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": PER_PAGE, "page": page})
        url = f"{API}/users/{urllib.parse.quote(login)}/starred?{query}"
        batch, link = request_json(url, token)
        if not isinstance(batch, list):
            raise SystemExit("GitHub API 返回了非预期格式。")
        items.extend(batch)
        print(f"  已读取第 {page} 页，共 {len(items)} 个 star", file=sys.stderr)
        if len(batch) < PER_PAGE or 'rel="next"' not in (link or ""):
            break
        page += 1
        if page > 200:
            break
    return items


def load_fixture(directory: Path):
    items = []
    for path in sorted(directory.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, dict):
            payload = payload.get("items", [])
        items.extend(payload)
    return items


def normalize(item: dict) -> dict:
    repo = item.get("repo") or item
    starred_at = item.get("starred_at") or repo.get("starred_at") or ""
    topics = repo.get("topics") or []
    if not isinstance(topics, list):
        topics = []
    license_info = repo.get("license")
    if isinstance(license_info, dict):
        license_name = license_info.get("spdx_id") or license_info.get("name") or ""
    elif isinstance(license_info, str):
        license_name = license_info
    else:
        license_name = ""
    if license_name in ("NOASSERTION", "NONE"):
        license_name = ""
    return {
        "full_name": repo.get("full_name") or "",
        "url": repo.get("html_url") or "",
        "description": collapse(repo.get("description")),
        "language": repo.get("language") or "",
        "topics": [str(topic).lower() for topic in topics],
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "archived": bool(repo.get("archived")),
        "homepage": repo.get("homepage") or "",
        "starred_at": starred_at,
        "created_at": repo.get("created_at") or "",
        "pushed_at": repo.get("pushed_at") or "",
        "open_issues": int(repo.get("open_issues_count") or 0),
        "license": license_name,
        "excerpt": "",
    }


# --------------------------------------------------------------- readme text


def fetch_readme_text(full_name: str) -> str:
    """Return the raw README of a public repo, or "" if there is none.

    raw.githubusercontent.com does not consume API quota. Most repos expose
    README.md, so the common case is a single request per repo per push.
    """
    for name in README_NAMES:
        url = f"{RAW}/{full_name}/HEAD/{name}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read(README_MAX_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            return ""
        except (urllib.error.URLError, OSError):
            return ""
        for encoding in (charset, "utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", "replace")
    return ""


def clean_readme(text: str) -> str:
    """Strip Markdown/HTML noise so the excerpt reads as plain prose."""
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", " ", text, flags=re.S)
    # Linked badges before plain images.
    text = re.sub(r"\[\!\[[^\]]*\]\([^)]*\)\]\([^)]*\)", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r":[a-z0-9_+\-]+:", " ", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}([=\-])\1{2,}\s*$", " ", text)
    # Table separator rows, then remaining pipes.
    text = re.sub(r"(?m)^\s*\|?[\s:\-|]+\|[\s:\-|]*$", " ", text)
    text = text.replace("|", " ")
    text = re.sub(r"(\*\*|__|`+)", "", text)
    text = re.sub(r"(?m)^\s{0,3}([-*+]|\d+[.)]|>)\s+", "", text)
    text = html.unescape(text)

    skip = {"table of contents", "table of content", "contents", "目录"}
    language_switcher = re.compile(
        r"(中文|english|日本語|한국어|français|deutsch|español|русский)"
        r"([\s|·/]+(中文|english|日本語|한국어|français|deutsch|español|русский))*"
    )
    lines = []
    for line in text.splitlines():
        line = collapse(line)
        if not line or line.lower().strip(" :-") in skip:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", line):
            continue
        if language_switcher.fullmatch(line.lower()):
            continue
        # Long "issue 1, issue 2, issue 3…" index lines are not prose.
        if len(re.findall(r"第\s*\d+\s*期", line)) >= 2:
            continue
        lines.append(line)
    return trim(" ".join(lines), README_EXCERPT)


def read_excerpt(repo: dict) -> str:
    """Fetch and clean, retrying once for transient network/rate-limit errors."""
    for attempt in range(2):
        excerpt = clean_readme(fetch_readme_text(repo["full_name"]))
        if excerpt:
            return excerpt
        if attempt == 0:
            time.sleep(1.0)
    return ""


def enrich_readmes(repos, root: Path, enabled: bool) -> None:
    """Attach README excerpts, cached by pushed_at to avoid repeated fetches."""
    if not enabled:
        for repo in repos:
            repo["excerpt"] = ""
        return

    cache_path = root / "data/readme_cache.json"
    cache = load_json(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}

    pending = []
    for repo in repos:
        entry = cache.get(repo["full_name"]) or {}
        if (
            repo["pushed_at"]
            and entry.get("pushed_at") == repo["pushed_at"]
            and entry.get("v") == README_CACHE_VERSION
            and entry.get("excerpt")
        ):
            repo["excerpt"] = entry.get("excerpt") or ""
        else:
            pending.append(repo)

    if pending:
        print(f"读取 README 摘要（{len(pending)} 个仓库）…", file=sys.stderr)
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(read_excerpt, repo): repo for repo in pending}
            for future in as_completed(futures):
                repo = futures[future]
                try:
                    repo["excerpt"] = future.result()
                except Exception:
                    repo["excerpt"] = ""
                done += 1
                if done % 10 == 0 or done == len(pending):
                    print(f"  已完成 {done}/{len(pending)}", file=sys.stderr)

    fresh = {}
    for repo in repos:
        if not repo["excerpt"]:
            # Do not cache failures: a later run should retry them.
            continue
        fresh[repo["full_name"]] = {
            "v": README_CACHE_VERSION,
            "pushed_at": repo["pushed_at"],
            "excerpt": repo["excerpt"],
        }
    cache_path.parent.mkdir(exist_ok=True)
    cache_path.write_text(
        json.dumps(fresh, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------- categorizing


class Classifier:
    def __init__(self, config: dict, overrides: dict):
        self.categories = config.get("categories", [])
        self.by_name = {
            category["name"].lower(): category for category in self.categories
        }
        self.overrides = {
            str(key).lower(): value
            for key, value in (overrides.get("categories") or {}).items()
        }
        self.notes = {
            str(key).lower(): str(value)
            for key, value in (overrides.get("notes") or {}).items()
        }
        self.pinned_order = [str(name).lower() for name in (overrides.get("pinned") or [])]
        self.pinned = set(self.pinned_order)

    def forced_category(self, full_name: str):
        raw = self.overrides.get(full_name.lower())
        if not raw:
            return None
        category = self.by_name.get(str(raw).lower())
        if not category:
            print(f"警告：overrides 里的分类「{raw}」不存在，已忽略。", file=sys.stderr)
            return None
        return category

    def match(self, repo: dict):
        forced = self.forced_category(repo["full_name"])
        if forced:
            return forced
        topics = set(repo["topics"])
        searchable = " ".join([repo["full_name"], repo["description"]]).lower()
        # Pass 1: exact topic match (most reliable).
        for category in self.categories:
            for keyword in category["keywords"]:
                if keyword.lower() in topics:
                    return category
        # Pass 2: long keywords in the name/description, word-bounded.
        for category in self.categories:
            for keyword in category["keywords"]:
                keyword = keyword.lower()
                if len(keyword) < 4:
                    continue
                pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
                if re.search(pattern, searchable):
                    return category
        return None

    def note(self, full_name: str) -> str:
        return self.notes.get(full_name.lower(), "")

    def is_pinned(self, full_name: str) -> bool:
        return full_name.lower() in self.pinned


def build_groups(repos, classifier: Classifier, config: dict):
    """Return ordered groups: curated categories first, then language buckets."""
    groups = []
    group_by_name = {}

    def add_group(heading, group_id, item=None):
        if heading not in group_by_name:
            group = {"heading": heading, "id": group_id, "items": []}
            group_by_name[heading] = group
            groups.append(group)
        if item:
            group_by_name[heading]["items"].append(item)

    for category in config.get("categories", []):
        heading = f'{category.get("emoji", "")} {category["name"]}'.strip()
        add_group(heading, category.get("id") or f"cat-{slug(category['name'])}")

    buckets = config.get("language_buckets", {})
    min_group = int(config.get("min_language_group", 2) or 1)
    other_heading = "📦 其他"
    language_items = []

    for repo in repos:
        category = classifier.match(repo)
        if category:
            heading = f'{category.get("emoji", "")} {category["name"]}'.strip()
            add_group(
                heading,
                category.get("id") or f"cat-{slug(category['name'])}",
                repo,
            )
        else:
            language_items.append(repo)

    # Count unclassified repos per language bucket.
    bucket_counts = {}
    for repo in language_items:
        heading = buckets.get(repo["language"])
        if not heading:
            heading = other_heading
        bucket_counts[heading] = bucket_counts.get(heading, 0) + 1

    ordered_buckets = [
        heading
        for heading, count in sorted(
            bucket_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )
        if heading != other_heading
        and (count >= min_group or heading in group_by_name)
    ]

    for repo in language_items:
        heading = buckets.get(repo["language"])
        if not heading or (heading not in ordered_buckets and heading not in group_by_name):
            heading = other_heading
        group_id = (
            "cat-other"
            if heading == other_heading
            else "lang-" + slug(repo["language"] or heading)
        )
        add_group(heading, group_id, repo)

    # Drop empty groups and sort items inside each group.
    final = []
    for group in groups:
        if not group["items"]:
            continue
        group["items"].sort(
            key=lambda repo: (-repo["stars"], repo["full_name"].lower())
        )
        final.append(group)
    return final


# ------------------------------------------------------------------ README


def item_line(repo, note: str, max_description: int) -> str:
    name = html.escape(repo["full_name"])
    line = f"- [**{name}**]({repo['url']})"
    if repo["archived"]:
        line += " 🗄️"
    chunks = []
    if note:
        chunks.append(f"📝 {html.escape(collapse(note), quote=False)}")
    if repo["description"]:
        chunks.append(html.escape(trim(repo["description"], max_description), quote=False))
    meta = []
    if repo["language"]:
        meta.append(f"`{repo['language']}`")
    if repo["stars"]:
        meta.append(f"★ {fmt_stars(repo['stars'])}")
    if repo["starred_at"]:
        meta.append(fmt_date(repo["starred_at"]))
    if meta:
        chunks.append(" · ".join(meta))
    if chunks:
        line += " — " + " · ".join(chunks)
    return line


def make_readme(login, repos, groups, classifier, config, site, now):
    max_description = int(config.get("max_description", 160))
    title = config.get("title", "我的 Star 收藏夹")
    subtitle = config.get("subtitle", "")
    total = len(repos)
    languages = {}
    total_stars = 0
    archived = 0
    for repo in repos:
        total_stars += repo["stars"]
        if repo["language"]:
            languages[repo["language"]] = languages.get(repo["language"], 0) + 1
        if repo["archived"]:
            archived += 1
    language_count = len(languages)

    recent = sorted(
        [repo for repo in repos if repo["starred_at"]],
        key=lambda repo: repo["starred_at"],
        reverse=True,
    )[:15]

    cutoff = now - timedelta(days=180)
    older = []
    for repo in repos:
        if classifier.is_pinned(repo["full_name"]) or not repo["starred_at"]:
            continue
        try:
            starred = datetime.fromisoformat(repo["starred_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if starred < cutoff:
            older.append(repo)
    rng = random.Random(f'{login}:{now.date().isoformat()}:revisit')
    revisit = rng.sample(older, min(6, len(older)))

    repo_by_name = {repo["full_name"].lower(): repo for repo in repos}
    pinned = [
        repo_by_name[name]
        for name in classifier.pinned_order
        if name in repo_by_name
    ]

    lines = []
    lines.append("<!-- 该文件由 scripts/build_index.py 自动生成，请勿直接编辑。 -->")
    lines.append("")
    lines.append(f"# ⭐ {title}")
    lines.append("")
    if subtitle:
        lines.append(f"> {subtitle}")
        lines.append(">")
    lines.append(
        f"> 自动整理自 [@{login} 的 Stars]({gh_link(login)})，"
        f"共 **{total}** 个仓库 · **{language_count}** 种语言 · "
        f"累计 **★ {fmt_stars(total_stars)}**"
    )
    lines.append(
        f"> 上次同步：**{now.strftime('%Y-%m-%d %H:%M')}**（UTC+8，每天自动更新）"
    )
    lines.append("")
    links = [f"[GitHub Stars]({gh_link(login)})"]
    if site.get("site_url"):
        links.append(f"[🔍 在线搜索]({site['site_url'].rstrip('/')}/)")
    links.append("[🛠️ 维护说明](#maintenance)")
    lines.append(" · ".join(links))
    lines.append("")

    if pinned:
        lines.append("## 📌 置顶")
        lines.append("")
        for repo in pinned:
            lines.append(item_line(repo, classifier.note(repo["full_name"]), max_description))
        lines.append("")

    if recent:
        lines.append(f"## 🕘 最近收藏 · {len(recent)}")
        lines.append("")
        for repo in recent:
            lines.append(item_line(repo, classifier.note(repo["full_name"]), max_description))
        lines.append("")

    if revisit:
        lines.append("## 🎲 遗忘角落")
        lines.append("")
        lines.append("> 从至少半年前收藏的项目里随机挑几个，每次同步换一批，帮你想起它们。")
        lines.append("")
        for repo in revisit:
            lines.append(item_line(repo, classifier.note(repo["full_name"]), max_description))
        lines.append("")

    lines.append("## 🧭 分类索引")
    lines.append("")
    for group in groups:
        lines.append(
            f"- [{group['heading']}](#{group['id']}) · {len(group['items'])}"
        )
    lines.append("")

    for group in groups:
        lines.append(f'<a name="{group["id"]}"></a>')
        lines.append(f"## {group['heading']} · {len(group['items'])}")
        lines.append("")
        for repo in group["items"]:
            lines.append(item_line(repo, classifier.note(repo["full_name"]), max_description))
        lines.append("")

    lines.append("## 📈 统计")
    lines.append("")
    top_languages = sorted(languages.items(), key=lambda pair: (-pair[1], pair[0]))[:10]
    lines.append(
        "- 语言分布：" + " · ".join(f"`{name}` {count}" for name, count in top_languages)
    )
    if archived:
        lines.append(f"- 已归档仓库：{archived} 个（仍保留在列表中，标注 🗄️）")
    oldest = min(
        (repo["starred_at"] for repo in repos if repo["starred_at"]),
        default="",
    )
    if oldest:
        lines.append(f"- 最早收藏：{fmt_date(oldest)}")
    lines.append("")

    lines.append('<a name="maintenance"></a>')
    lines.append("## 🛠️ 维护说明")
    lines.append("")
    lines.append(
        "- 自动同步：GitHub Actions 每天运行一次；也可以在仓库的 **Actions → Sync stars → Run workflow** 手动触发。"
    )
    lines.append(
        "- 加笔记：编辑 [`config/overrides.json`](config/overrides.json) 里的 `notes`，下次同步会显示在对应项目后面。"
    )
    lines.append(
        "- 改分类：在 `overrides.json` 的 `categories` 里写 `\"仓库全名\": \"分类名\"`，可覆盖自动分类。"
    )
    lines.append(
        "- 置顶：把仓库全名加入 `overrides.json` 的 `pinned` 数组。"
    )
    if site.get("site_url"):
        lines.append(f"- 搜索页：[{site['site_url'].rstrip('/')}/]({site['site_url'].rstrip('/')}/)")
    else:
        lines.append(
            "- 搜索页：在仓库 **Settings → Pages** 选择 `main` 分支的 `/docs` 目录，然后把地址填进 `config/site.json` 的 `site_url`。"
        )
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- site


def make_site_data(login, repos, groups, classifier, now):
    category_of = {}
    for group in groups:
        for repo in group["items"]:
            category_of[repo["full_name"]] = group["heading"]
    ordered = sorted(
        repos, key=lambda repo: (repo["starred_at"] or ""), reverse=True
    )
    stars = []
    for repo in ordered:
        stars.append(
            {
                "full_name": repo["full_name"],
                "url": repo["url"],
                "description": repo["description"],
                "language": repo["language"],
                "topics": repo["topics"],
                "stars": repo["stars"],
                "forks": repo["forks"],
                "archived": repo["archived"],
                "homepage": repo["homepage"],
                "starred_at": repo["starred_at"],
                "created_at": repo["created_at"],
                "pushed_at": repo["pushed_at"],
                "open_issues": repo["open_issues"],
                "license": repo["license"],
                "excerpt": repo["excerpt"],
                "category": category_of.get(repo["full_name"], "📦 其他"),
                "note": classifier.note(repo["full_name"]),
            }
        )
    return {
        "login": login,
        "generated_at": now.isoformat(timespec="minutes"),
        "count": len(stars),
        "categories": [
            {"heading": group["heading"], "count": len(group["items"])}
            for group in groups
        ],
        "stars": stars,
    }


def make_site_html(template: str, site_data, title, subtitle, now) -> str:
    payload = json.dumps(site_data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return (
        template.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__LOGIN__", html.escape(site_data["login"]))
        .replace("__GENERATED_AT__", now.strftime("%Y-%m-%d %H:%M UTC+8"))
        .replace("__DATA__", payload)
    )


# -------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", default="", help="GitHub username")
    parser.add_argument("--token", default="", help="optional GitHub token")
    parser.add_argument("--fixture", type=Path, help="read API payloads from a directory")
    parser.add_argument("--no-readme", action="store_true", help="skip README excerpt fetching")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    root = args.root.resolve()
    config = load_json(root / "config/categories.json", {})
    overrides = load_json(root / "config/overrides.json", {})
    site = load_json(root / "config/site.json", {})

    login = args.login or detect_login()
    if not login and not args.fixture:
        raise SystemExit(
            "找不到 GitHub 用户名：请加 --login yourname，或设置 GH_LOGIN 环境变量。"
        )
    login = login or "demo-user"

    token = args.token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    print(f"读取 @{login} 的公开 star…", file=sys.stderr)
    raw = load_fixture(args.fixture) if args.fixture else fetch_all(login, token)
    repos = [normalize(item) for item in raw]
    repos = [repo for repo in repos if repo["full_name"]]
    print(f"共 {len(repos)} 个仓库", file=sys.stderr)
    enrich_readmes(repos, root, not args.no_readme)

    classifier = Classifier(config, overrides)
    groups = build_groups(repos, classifier, config)
    now = datetime.now(BEIJING)

    readme = make_readme(login, repos, groups, classifier, config, site, now)
    (root / "README.md").write_text(readme + "\n", encoding="utf-8")

    data = make_site_data(login, repos, groups, classifier, now)
    (root / "data").mkdir(exist_ok=True)
    (root / "data/stars.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    template_path = root / "scripts/site_template.html"
    template = template_path.read_text(encoding="utf-8")
    site_html = make_site_html(
        template, data, config.get("title", "Star 收藏夹"),
        config.get("subtitle", ""), now
    )
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "index.html").write_text(site_html, encoding="utf-8")
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    print(
        f"完成：README.md、data/stars.json、docs/index.html（{len(groups)} 个分类）",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
