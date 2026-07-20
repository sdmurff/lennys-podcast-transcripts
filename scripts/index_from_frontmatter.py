#!/usr/bin/env python3
"""
Build/update the topic index in index/ from each episode's frontmatter `keywords`.

Deterministic and idempotent: no Claude calls (unlike scripts/build-index.sh, which
generates keywords on the fly). Episodes ingested by sync.py already carry keywords in
their frontmatter, so this is the fast, reliable way to keep the index current. Episodes
without frontmatter keywords are left untouched (their entries, if any, already exist in
the committed index; run build-index.sh once if you need to generate keywords for them).

Output format matches build-index.sh exactly:
    index/{keyword-slug}.md  ->  "- [Guest](../episodes/{slug}/transcript.md)"
    index/README.md          ->  topic list with per-topic episode counts

Usage: python3 scripts/index_from_frontmatter.py [--date YYYY-MM-DD]
"""

import argparse
import datetime
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPISODES_DIR = os.path.join(REPO_ROOT, "episodes")
INDEX_DIR = os.path.join(REPO_ROOT, "index")


def keyword_filename(keyword):
    """Match build-index.sh: lowercase, ' ' and '/' -> '-', keep only [a-z0-9-]."""
    s = keyword.lower().replace(" ", "-").replace("/", "-")
    return re.sub(r"[^a-z0-9-]", "", s)


def read_frontmatter(path):
    text = open(path, encoding="utf-8").read()
    if not text.startswith("---"):
        return None, []
    fm = text.split("---", 2)[1]
    guest_m = re.search(r'^guest:\s*"?(.*?)"?\s*$', fm, re.M)
    guest = guest_m.group(1).strip() if guest_m else None
    kw_block = re.search(r"^keywords:\s*\n((?:-\s.*\n?)+)", fm, re.M)
    keywords = []
    if kw_block:
        for line in kw_block.group(1).splitlines():
            m = re.match(r'^-\s*"?(.*?)"?\s*$', line)
            if m and m.group(1):
                keywords.append(m.group(1).strip())
    return guest, keywords


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--only", help="Comma/space separated slugs to index (default: all "
                                    "episodes whose frontmatter has keywords). Use this "
                                    "to add just newly-synced episodes without re-indexing "
                                    "the curated existing index.")
    args = ap.parse_args()

    only = set(re.split(r"[,\s]+", args.only.strip())) if args.only else None

    added = 0
    for slug in sorted(os.listdir(EPISODES_DIR)):
        if only is not None and slug not in only:
            continue
        tpath = os.path.join(EPISODES_DIR, slug, "transcript.md")
        if not os.path.isfile(tpath):
            continue
        guest, keywords = read_frontmatter(tpath)
        if not guest or not keywords:
            continue
        link = f"- [{guest}](../episodes/{slug}/transcript.md)"
        ref = f"episodes/{slug}/transcript.md"
        for kw in keywords:
            fname = keyword_filename(kw)
            if not fname:
                continue
            kpath = os.path.join(INDEX_DIR, f"{fname}.md")
            if os.path.isfile(kpath):
                content = open(kpath, encoding="utf-8").read()
                if ref in content:  # idempotent: already listed
                    continue
                if not content.endswith("\n"):
                    content += "\n"
                open(kpath, "w", encoding="utf-8").write(content + link + "\n")
            else:
                header = f"# {kw}\n\nEpisodes discussing **{kw}**:\n\n"
                open(kpath, "w", encoding="utf-8").write(header + link + "\n")
            added += 1

    # Rebuild README.md (mirrors build-index.sh).
    kfiles = sorted(
        f for f in os.listdir(INDEX_DIR)
        if f.endswith(".md") and f not in ("README.md", "episodes.md")
    )
    unique = set()
    for f in kfiles:
        for m in re.finditer(r"episodes/[^)]*?/transcript\.md", open(os.path.join(INDEX_DIR, f), encoding="utf-8").read()):
            unique.add(m.group(0))
    lines = [
        "# Lenny's Podcast Index",
        "",
        f"*Generated: {args.date} | {len(unique)} episodes indexed*",
        "",
        "## Topics",
        "",
    ]
    for f in kfiles:
        name = f[:-3]
        title = " ".join(w.capitalize() for w in name.split("-"))
        count = len(re.findall(r"^- \[", open(os.path.join(INDEX_DIR, f), encoding="utf-8").read(), re.M))
        lines.append(f"- [{title}]({name}.md) ({count} episodes)")
    open(os.path.join(INDEX_DIR, "README.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")

    print(f"Added {added} topic entries. {len(unique)} unique episodes indexed.")


if __name__ == "__main__":
    main()
