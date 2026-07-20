# Keeping this fork updated

This is a fork of [ChatPRD/lennys-podcast-transcripts](https://github.com/ChatPRD/lennys-podcast-transcripts).
Upstream is dormant, so this fork updates itself directly from **Lenny's own public
Dropbox folder**, which he keeps current as each new episode airs (per his LinkedIn
post sharing the archive).

## How it works

`scripts/sync.py` (standard library only, no pip install needed):

1. Downloads Lenny's Dropbox folder as a zip (`&dl=1`, no auth) and reads the `.txt`
   transcripts.
2. Compares against the episodes already in `episodes/` and keeps only the new ones.
3. Matches each new transcript to its YouTube episode to pull metadata
   (title, `youtube_url`, `publish_date`, `duration`, `view_count`, `description`):
   - primary: the `@LennysPodcast` channel video list (via `yt-dlp`), matched on the
     guest name parsed from the video title;
   - fallback: a YouTube search, confirmed by checking the guest's bio line in the
     video description (for episodes whose title omits the guest name).
4. Writes `episodes/{slug}/transcript.md` in the same layout and frontmatter schema
   as the rest of the repo.
5. Optionally generates the `keywords` frontmatter with the Claude CLI (same prompt as
   `scripts/build-index.sh`).

Transcripts that can't be confidently matched to a video are still ingested with their
text and a minimal frontmatter, and reported as unmatched for manual review.

## Run it locally

```bash
# Requires: yt-dlp, python3. For keywords/index also: the Claude CLI, logged in.
python3 scripts/sync.py --dry-run     # show what would be added, write nothing
python3 scripts/sync.py               # ingest new episodes (+ keywords)
python3 scripts/sync.py --no-keywords # metadata only, no Claude calls
./scripts/build-index.sh              # rebuild the topic index in index/
```

## Automated updates

`.github/workflows/sync.yml` runs `sync.py` weekly (and on manual dispatch), then
commits and pushes any new episodes. The routine run is metadata-only, so it needs no
API key or extra dependencies beyond `yt-dlp`. To also generate `keywords` in CI, add
an `ANTHROPIC_API_KEY` secret and run the workflow manually with the "keywords" input
enabled.

## Notes

- Re-records (Lenny names them e.g. `Elizabeth Stone 2.0.txt`) become their own slug
  (`elizabeth-stone-20`) and are matched to the newer video; the original episode keeps
  its original video.
- The topic keyword model in `sync.py` and `build-index.sh` is
  `claude-haiku-4-5-20251001` (the upstream default, Sonnet 4, was retired).
