#!/usr/bin/env python3
"""
Sync new Lenny's Podcast transcripts from Lenny's public Dropbox folder into this
repo, in the same structure Claire Vo's original repo uses:

    episodes/{slug}/transcript.md   with YAML frontmatter + "# title / ## Transcript / body"

Source of truth for transcript TEXT is Lenny's Dropbox folder (he keeps it updated
as each episode airs). Metadata (title, youtube_url, publish_date, duration,
view_count, description) is matched and enriched from the Lenny's Podcast YouTube
channel via yt-dlp. Topic keywords are generated with the Claude CLI, using the
same prompt as scripts/build-index.sh.

Stdlib only (no pip install), so it runs unmodified in CI.

Usage:
    python3 scripts/sync.py --dry-run          # show the plan, write nothing
    python3 scripts/sync.py                     # ingest new episodes (enrich + keywords)
    python3 scripts/sync.py --no-keywords       # skip the Claude keyword step
    python3 scripts/sync.py --limit 5           # cap number of new episodes (testing)

Inputs (all auto-fetched if not supplied):
    --dropbox-dir DIR    directory of Lenny's *.txt files (else download the zip)
    --channel-json FILE  yt-dlp flat channel dump (else run yt-dlp)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

# Lenny's public Dropbox folder (from his LinkedIn post). dl=1 => whole-folder zip.
DROPBOX_ZIP_URL = (
    "https://www.dropbox.com/scl/fo/yxi4s2w998p1gvtpu4193/"
    "AMdNPR8AOw0lMklwtnC0TrQ?rlkey=j06x0nipoti519e0xgm23zsn9&dl=1"
)
CHANNEL_URL = "https://www.youtube.com/@LennysPodcast/videos"
CHANNEL_NAME = "Lenny's Podcast"
KEYWORD_MODEL = "claude-haiku-4-5-20251001"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPISODES_DIR = os.path.join(REPO_ROOT, "episodes")

# Files in the Dropbox that are not single-guest interview episodes; ingest the
# text but don't expect a clean 1:1 YouTube match.
NON_INTERVIEW = {
    "failure", "eoyreview", "teaser2021", "interviewqcompilation",
}

# Manual video_id pins for episodes whose YouTube title omits the guest name AND whose
# search match is unreliable (topic-only titles, spelling variants). Keyed by the
# Dropbox filename stem. Verified by hand against the video's description.
OVERRIDES = {
    "Sam Lessin": "KtKJ3A6DWTs",      # "How to show up in any room with a low heart rate"
    "Max Schoenig": "mCO-D3pkviM",    # YouTube spells it "Max Schoening (Notion)"
    "Michelle Rial": "mlbWjGZiG20",   # "A guide to getting out of a creative slump"
}

VERSION_RE = r"[\s_.-]+v?[2-9]\.0(?=$|[\s_.-])|[\s_.-]+v[2-9](?=$|[\s_.-])"


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #
def slugify(name):
    """Guest name -> repo slug, matching Claire's convention.

    "Elizabeth Stone 2.0" -> "elizabeth-stone-20"
    "Aishwarya Naresh Reganti + Kiriti Badam" -> "aishwarya-naresh-reganti-kiriti-badam"
    "Casey Winters_" -> "casey-winters_"
    """
    s = name.lower()
    s = s.replace("&", " ").replace("+", " ")
    s = s.replace(".", "").replace(",", "")
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)  # keep word chars (incl. _), space, hyphen
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def norm(name):
    """Lossy key for dedup / fuzzy matching: alnum lowercase only. Keeps digits so
    a "2.0" re-record stays distinct from its original."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def strip_version(name):
    """Drop a trailing version marker so 're-record' files match the guest's videos.
    Handles ' 2.0', '_2.0', '-2.0', '_V2', etc."""
    return re.sub(VERSION_RE, "", name, flags=re.IGNORECASE).strip(" _-")


def is_rerecord(name):
    return bool(re.search(VERSION_RE, name, flags=re.IGNORECASE))


def guest_from_title(title):
    """Lenny titles are usually '<hook> | <Guest> (<role>)' but sometimes
    '<Guest>: <hook>'. Pull the guest from whichever form applies."""
    if "|" in title:
        guest = title.rsplit("|", 1)[1]
    elif ":" in title:
        before = title.split(":", 1)[0].strip()
        words = before.split()
        # Treat as a name only if it's short and each alpha word is capitalized.
        if 1 <= len(words) <= 4 and all(w[0].isupper() for w in words if w[:1].isalpha()):
            guest = before
        else:
            return None
    else:
        return None
    guest = re.sub(r"\([^)]*\)", "", guest)  # drop "(CPTO)", "(Netflix)", etc.
    guest = re.sub(r"\b(CPO|CTO|CEO|CPTO|COO|VP|Co-?founder)\b", "", guest, flags=re.IGNORECASE)
    return guest.strip(" ,-·")


def fmt_duration(seconds):
    if not seconds:
        return None
    seconds = int(float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def yaml_scalar(value):
    """Emit a value as a safe single-line YAML scalar (stdlib, no PyYAML)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    s = str(value)
    if s == "":
        return '""'
    sexagesimal = bool(re.fullmatch(r"[\d:]+", s))  # "1:08:29" parses as base-60 unless quoted
    needs_quote = (
        s[0] in "!&*?{}[],#|>@`\"'%- " or s[-1] in " :"
        or ": " in s or " #" in s or "\t" in s or "\n" in s or sexagesimal
    )
    if not needs_quote:
        return s
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{esc}"'


# --------------------------------------------------------------------------- #
# Data acquisition
# --------------------------------------------------------------------------- #
def fetch_dropbox(dropbox_dir, workdir):
    if dropbox_dir:
        return dropbox_dir
    dest = os.path.join(workdir, "dropbox")
    os.makedirs(dest, exist_ok=True)
    zip_path = os.path.join(workdir, "dropbox.zip")
    print(f"Downloading Lenny's Dropbox folder ...", file=sys.stderr)
    req = urllib.request.Request(DROPBOX_ZIP_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as r, open(zip_path, "wb") as f:
        f.write(r.read())
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.endswith(".txt"):
                # flatten any path, drop the leading "/" entry Dropbox includes
                target = os.path.join(dest, os.path.basename(member))
                with z.open(member) as src, open(target, "wb") as out:
                    out.write(src.read())
    n = len([f for f in os.listdir(dest) if f.endswith(".txt")])
    print(f"  {n} transcripts in Dropbox", file=sys.stderr)
    return dest


def fetch_channel(channel_json, workdir):
    if channel_json:
        with open(channel_json) as f:
            return [json.loads(line) for line in f if line.strip()]
    print("Dumping YouTube channel video list (yt-dlp, flat) ...", file=sys.stderr)
    out = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--dump-json", CHANNEL_URL],
        capture_output=True, text=True, check=True,
    ).stdout
    vids = [json.loads(line) for line in out.splitlines() if line.strip()]
    print(f"  {len(vids)} videos on channel", file=sys.stderr)
    return vids


def enrich_video(video_id, cache):
    """Full metadata for one video (upload_date, view_count, description, duration)."""
    if video_id in cache:
        return cache[video_id]
    try:
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--dump-json",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        print(f"  ! enrich failed for {video_id}: {e}", file=sys.stderr)
        data = {}
    cache[video_id] = data
    return data


def generate_keywords(transcript_body, model):
    """Same prompt/model as build-index.sh; returns a list (possibly empty)."""
    prompt = (
        "Analyze this podcast transcript and provide 4-6 BROAD topic keywords.\n\n"
        "Rules:\n"
        "- Use general categories, not specific terms (e.g., \"AI\" not \"ai-coding-agents\", "
        "\"leadership\" not \"servant-leadership\")\n"
        "- Prefer these existing topics when relevant: product management, leadership, growth "
        "strategy, product-led growth, product strategy, product development, career development, "
        "experimentation, hiring, entrepreneurship, startup growth, venture capital, team building, "
        "company culture, decision making, AI, machine learning, remote work, OKRs, mentorship, "
        "storytelling, innovation, feedback, retention, user experience, network effects\n"
        "- Only include company names if they are a major focus (e.g., Airbnb, Google, Meta, "
        "Stripe, Uber, Slack, Facebook)\n\n"
        'Output ONLY valid JSON: {"keywords": ["...", "..."]}\n\n'
        "TRANSCRIPT:\n" + transcript_body
    )
    try:
        out = subprocess.run(
            ["claude", "--model", model, "-p"],
            input=prompt, capture_output=True, text=True, timeout=180,
        ).stdout
        m = re.search(r"\{.*\}", out, re.DOTALL)
        if m:
            return json.loads(m.group(0)).get("keywords", []) or []
    except Exception as e:
        print(f"  ! keyword generation failed: {e}", file=sys.stderr)
    return []


# --------------------------------------------------------------------------- #
# Core
# --------------------------------------------------------------------------- #
def existing_index():
    """Return (set of slugs, set of alnum-norms, set of used video_ids)."""
    slugs, norms, used_ids = set(), set(), set()
    if not os.path.isdir(EPISODES_DIR):
        return slugs, norms, used_ids
    for slug in os.listdir(EPISODES_DIR):
        d = os.path.join(EPISODES_DIR, slug)
        if not os.path.isdir(d):
            continue
        slugs.add(slug)
        norms.add(norm(slug))
        tpath = os.path.join(d, "transcript.md")
        if os.path.isfile(tpath):
            with open(tpath, encoding="utf-8", errors="ignore") as f:
                head = f.read(2000)
            m = re.search(r"^video_id:\s*(\S+)", head, re.MULTILINE)
            if m:
                used_ids.add(m.group(1).strip())
    return slugs, norms, used_ids


def build_video_index(videos):
    """guest-norm -> list of videos (channel order = newest first)."""
    idx = {}
    for v in videos:
        title = v.get("title") or ""
        guest = guest_from_title(title)
        if not guest:
            continue
        idx.setdefault(norm(guest), []).append(v)
    return idx


def match_video(dropbox_name, video_index, used_ids):
    """Best YouTube video for a Dropbox transcript filename, from the channel index."""
    key = norm(strip_version(dropbox_name))
    candidates = video_index.get(key)
    if not candidates:
        # Containment: role-prefixed guest ("...Brian Halligan") or a topic suffix
        # on the filename ("Claire Vo OpenClaw"). Pick the most specific (longest) key.
        best = None
        for vkey, vids in video_index.items():
            if len(vkey) >= 7 and len(key) >= 7 and (vkey in key or key in vkey):
                if best is None or len(vkey) > len(best[0]):
                    best = (vkey, vids)
        candidates = best[1] if best else None
    if not candidates:
        return None
    for v in candidates:  # newest video (channel order) not already used
        if v.get("id") not in used_ids:
            return v
    return candidates[0]


def search_and_verify(dropbox_name, used_ids, enrich_cache):
    """Fallback for episodes whose YouTube title omits the guest name (topic/role-only
    titles). Search YouTube, keep only Lenny's Podcast uploads, and confirm the guest
    by checking their name appears in the video description's bio line."""
    base = strip_version(dropbox_name)
    tokens = [t for t in re.split(r"[\s_]+", base) if t.isalpha()]
    if len(tokens) < 2:
        return None, {}
    probe = norm(" ".join(tokens[:2]))  # e.g. "simonwillison", "clairevo"
    try:
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", f"ytsearch8:Lenny's Podcast {base}"],
            capture_output=True, text=True, timeout=120,
        ).stdout
    except subprocess.SubprocessError:
        return None, {}
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        channel = (r.get("channel") or r.get("uploader") or "")
        vid = r.get("id")
        if "lenny" not in channel.lower() or not vid or vid in used_ids:
            continue
        meta = enrich_video(vid, enrich_cache)
        if "lenny" not in (meta.get("uploader") or meta.get("channel") or "").lower():
            continue
        # Confirm the guest is the SUBJECT: Lenny's bios lead with "<Name> is/was ...".
        # A mere mention mid-description (e.g. a guest named in another episode's blurb)
        # must not count.
        desc = (meta.get("description") or "").lstrip()
        if norm(desc[:60]).startswith(probe):
            return {"id": vid, "title": meta.get("title")}, meta
    return None, {}


def compact_body(raw_text):
    """Match Claire's body: drop blank lines between speaker turns, trim trailing ws."""
    lines = [ln.rstrip() for ln in raw_text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip())


def build_frontmatter(guest, video, meta):
    fm = {
        "guest": guest,
        "title": (meta.get("title") or video.get("title") if video else guest) or guest,
    }
    if video:
        vid = video.get("id")
        fm["youtube_url"] = f"https://www.youtube.com/watch?v={vid}"
        fm["video_id"] = vid
        upload = meta.get("upload_date")
        if upload and len(upload) == 8:
            fm["publish_date"] = f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
        desc = (meta.get("description") or "").strip()
        if desc:
            first = desc.split("\n", 1)[0].strip()
            if len(first) > 300:
                cut = first[:300].rsplit(" ", 1)[0]
                first = cut + "..."
            fm["description"] = first
        dur = meta.get("duration") or video.get("duration")
        if dur:
            fm["duration_seconds"] = float(dur)
            fm["duration"] = fmt_duration(dur)
        if meta.get("view_count") is not None:
            fm["view_count"] = int(meta["view_count"])
        fm["channel"] = CHANNEL_NAME
    return fm


def render(fm, title, body, keywords):
    out = ["---"]
    for k, v in fm.items():
        out.append(f"{k}: {yaml_scalar(v)}")
    if keywords:
        out.append("keywords:")
        for kw in keywords:
            out.append(f"- {yaml_scalar(kw)}")
    else:
        out.append("keywords: []")
    out.append("---")
    out.append(f"# {title}")
    out.append("## Transcript")
    out.append(body)
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dropbox-dir")
    ap.add_argument("--channel-json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-keywords", action="store_true")
    ap.add_argument("--keyword-model", default=KEYWORD_MODEL)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="lenny-sync-")
    dropbox_dir = fetch_dropbox(args.dropbox_dir, workdir)
    videos = fetch_channel(args.channel_json, workdir)
    video_index = build_video_index(videos)

    slugs, norms, used_ids = existing_index()
    print(f"Existing episodes: {len(slugs)}", file=sys.stderr)

    txts = sorted(f for f in os.listdir(dropbox_dir) if f.endswith(".txt"))
    new_items = []
    for fname in txts:
        name = fname[:-4]  # strip .txt
        slug = slugify(name)
        if slug in slugs or norm(name) in norms:
            continue
        new_items.append((name, slug, fname))

    print(f"Dropbox transcripts: {len(txts)}  |  NEW: {len(new_items)}", file=sys.stderr)
    if args.limit:
        new_items = new_items[: args.limit]

    enrich_cache = {}
    matched = unmatched = written = 0

    for name, slug, fname in new_items:
        n = norm(strip_version(name))
        video, src = None, ""
        if name in OVERRIDES:
            video, src = {"id": OVERRIDES[name], "title": None}, "override"
        elif n not in NON_INTERVIEW:
            video = match_video(name, video_index, used_ids)
            src = "channel"
            if not video:
                video, _ = search_and_verify(name, used_ids, enrich_cache)
                src = "search"
        if video:
            matched += 1
            used_ids.add(video.get("id"))
            tag = f'[{src}] -> "{video.get("title")}" [{video.get("id")}]'
        else:
            unmatched += 1
            tag = "-> (no YouTube match; text only)"
        print(f"  NEW {slug:<40} {tag}")

        if args.dry_run:
            continue

        meta = enrich_video(video["id"], enrich_cache) if video else {}
        with open(os.path.join(dropbox_dir, fname), encoding="utf-8", errors="ignore") as f:
            body = compact_body(f.read())
        guest = name if not video else guest_from_title(video.get("title") or "") or name
        fm = build_frontmatter(guest, video, meta)
        title = fm["title"]
        keywords = [] if args.no_keywords else generate_keywords(body, args.keyword_model)

        dest_dir = os.path.join(EPISODES_DIR, slug)
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, "transcript.md"), "w", encoding="utf-8") as f:
            f.write(render(fm, title, body, keywords))
        written += 1

    print(f"\nSummary: {len(new_items)} new | matched {matched} | "
          f"unmatched {unmatched} | written {written}"
          + (" (dry-run)" if args.dry_run else ""), file=sys.stderr)


if __name__ == "__main__":
    main()
