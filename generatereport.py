import os
import sys
import re
import html
import time
import random
import shutil
import sqlite3
import difflib
import tempfile
import threading
import subprocess
import urllib.parse
from datetime import datetime, timezone

import pymysql
import requests
from bs4 import BeautifulSoup
from dateutil.parser import parse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# USER CONFIGURATION (Tweakable parameters — no need to edit below this block)
# ==============================================================================

SCRIPT_VERSION = "1.0.0"

# Number of Spoken Wikipedia articles to include in the report.
# Set to 10 (or another small integer) for fast development/testing runs.
# Set to 0 to process ALL Spoken Wikipedia articles (~1,800+).
ARTICLE_LIMIT = 10

# How articles are chosen when ARTICLE_LIMIT > 0:
#   "alphabetical" : Deterministic first N articles alphabetically (repeatable tests).
#   "random"       : Random sample of N articles on each run (good for spot-checking).
ARTICLE_SELECTION_MODE = "alphabetical"

# Output path for the generated static HTML report.
# On Toolforge, Lighttpd serves /data/project/spokenreport/public_html/index.html.
# If /data/project/spokenreport does not exist (e.g. local test), falls back to ./output/index.html.
TOOLFORGE_HOME = "/data/project/spokenreport"
OUTPUT_HTML_PATH = (
    f"{TOOLFORGE_HOME}/public_html/index.html"
    if os.path.isdir(TOOLFORGE_HOME)
    else os.path.abspath("./output/index.html")
)

# Persistent SQLite cache for audio LUFS measurements (keyed by file SHA1) and
# spoken-prose similarity scores so repeat runs don't re-stream unchanged files.
CACHE_DB_PATH = (
    f"{TOOLFORGE_HOME}/spokenreport_cache.sqlite"
    if os.path.isdir(TOOLFORGE_HOME)
    else os.path.abspath("./output/spokenreport_cache.sqlite")
)

# Feature toggles:
ENABLE_AUDIO_LUFS = True          # Stream audio through ffmpeg ebur128 to measure LUFS/Peak/LRA.
FORCE_REANALYZE_AUDIO = False     # Ignore SQLite cache and re-measure audio LUFS on every run.
ENABLE_PROSE_SIMILARITY = True    # Fetch Parsoid HTML of old & current revisions to score spoken text match %.

# Loudness quality-control thresholds (spoken word/podcasts typically target -16 to -19 LUFS):
LUFS_TOO_QUIET_THRESHOLD = -23.0  # Integrated LUFS below this flags the recording as "Quiet".
LUFS_TOO_LOUD_THRESHOLD = -12.0   # Integrated LUFS above this flags the recording as "Loud".
TRUE_PEAK_CLIP_THRESHOLD = -0.2   # True Peak (dBFS) above this flags potential digital clipping.
LUFS_DUAL_MONO = False            # Set True to apply EBU R128 dual-mono (+3 LU) compensation on 1-ch files.

# Article text drift alert thresholds (used for UI badges and quick-filter buttons):
PROSE_SIMILARITY_WARN_BELOW = 75.0  # Flag "High Drift" when spoken prose similarity drops below this %.
ABS_KB_WARN_ABOVE = 15.0            # Flag "High Drift" if prose similarity is off/unavailable and |Δ KB| >= this.

# Default table sort when opening the HTML page in a browser:
# Options: "article", "date", "edits", "abs_kb", "prose_sim", "lufs"
DEFAULT_SORT_COLUMN = "abs_kb"
DEFAULT_SORT_DIRECTION = "desc"   # "asc" or "desc"

# Audio discovery & streaming network parameters:
# Files explicitly listed on Wikipedia:Spoken_articles always pass; heuristic matches require MIN_FILE_SIZE_BYTES.
MIN_FILE_SIZE_BYTES = 500_000
AUDIO_STREAM_SLEEP_SECONDS = 0.5  # Polite pause between uncached audio downloads from upload.wikimedia.org.
AUDIO_TIMEOUT_SECONDS = 300       # Maximum seconds to stream/analyze a single audio file before timing out.

# Wikimedia Toolforge replica & User-Agent settings:
USER_AGENT = (
    f"SpokenWikipediaReport/{SCRIPT_VERSION} "
    "(https://spokenreport.toolforge.org/; https://github.com/xenotropic/spokenreport; joe@morris.cloud)"
)
ENWIKI_DB_HOST = "enwiki.analytics.db.svc.wikimedia.cloud"
ENWIKI_DB_NAME = "enwiki_p"
COMMONS_DB_HOST = "commonswiki.analytics.db.svc.wikimedia.cloud"
COMMONS_DB_NAME = "commonswiki_p"
REPLICA_CNF_PATH = os.path.expanduser("~/replica.my.cnf")

# Optional link to SpokenView in each row for convenient re-reading:
SPOKENVIEW_BASE_URL = "https://spokenview.toolforge.org/view/"

# ==============================================================================
# END OF USER CONFIGURATION
# ==============================================================================


def to_str(val):
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return str(val)


def create_http_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def get_db_connection(host=ENWIKI_DB_HOST, database=ENWIKI_DB_NAME):
    return pymysql.connect(
        host=host,
        database=database,
        read_default_file=REPLICA_CNF_PATH,
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_cache_db(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audio_lufs_cache (
            audio_file TEXT NOT NULL,
            sha1 TEXT NOT NULL,
            dual_mono INTEGER NOT NULL,
            lufs REAL,
            lra REAL,
            true_peak REAL,
            measured_at TEXT NOT NULL,
            PRIMARY KEY (audio_file, sha1, dual_mono)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prose_sim_cache (
            page_id INTEGER NOT NULL,
            base_rev_id INTEGER NOT NULL,
            curr_rev_id INTEGER NOT NULL,
            similarity REAL NOT NULL,
            computed_at TEXT NOT NULL,
            PRIMARY KEY (page_id, base_rev_id, curr_rev_id)
        )
        """
    )
    conn.commit()
    return conn


def normalize_wiki_title(title):
    if not title:
        return ""
    cleaned = urllib.parse.unquote(html.unescape(title))
    cleaned = cleaned.replace("\u200e", "").replace("\u200f", "").strip()
    cleaned = re.sub(r"'{2,5}", "", cleaned)
    cleaned = cleaned.replace(" ", "_")
    if not cleaned:
        return ""
    return cleaned[0].upper() + cleaned[1:]


def fetch_spoken_records():
    sql = """
    SELECT DISTINCT
        p.page_id,
        p.page_latest,
        CONVERT(p.page_title USING utf8mb4) AS article_title,
        CONVERT(lt_img.lt_title USING utf8mb4) AS audio_file
    FROM categorylinks cl
    JOIN linktarget lt_cat ON cl.cl_target_id = lt_cat.lt_id
    JOIN page p ON cl.cl_from = p.page_id
    JOIN imagelinks il ON il.il_from = p.page_id
    JOIN linktarget lt_img ON il.il_target_id = lt_img.lt_id
    WHERE lt_cat.lt_title = 'Spoken_articles'
      AND lt_cat.lt_namespace = 14
      AND p.page_namespace = 0
      AND lt_img.lt_namespace = 6;
    """
    conn = get_db_connection(ENWIKI_DB_HOST, ENWIKI_DB_NAME)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    allowed_exts = (".ogg", ".oga", ".mp3", ".flac", ".wav", ".opus")
    results = []
    for r in rows:
        audio = to_str(r["audio_file"])
        if audio.lower().endswith(allowed_exts):
            results.append(
                {
                    "page_id": int(r["page_id"]),
                    "page_latest": int(r["page_latest"]),
                    "article": to_str(r["article_title"]),
                    "audio": audio,
                }
            )
    return results


def fetch_index_whitelist(session):
    url = "https://en.wikipedia.org/w/api.php"
    params = {
        "action": "parse",
        "page": "Wikipedia:Spoken_articles",
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
    }
    whitelist = set()
    try:
        res = session.get(url, params=params, timeout=20)
        if res.status_code == 200:
            data = res.json().get("parse", {}).get("wikitext", "")
            wikitext = data.get("*", "") if isinstance(data, dict) else data
            matches = re.findall(
                r"\[\[:?(?:File|Image|Media):([^|\]]+\.(?:ogg|oga|mp3|flac|wav|opus))",
                wikitext,
                re.IGNORECASE,
            )
            for raw_fname in matches:
                fname = normalize_wiki_title(raw_fname)
                if fname:
                    whitelist.add(fname)
    except Exception as exc:
        print(f"Warning: could not fetch Wikipedia:Spoken_articles whitelist: {exc}", file=sys.stderr)
    return whitelist


def prefilter_candidates(raw_records, whitelist_set):
    by_article = {}
    seen = set()
    for rec in raw_records:
        pair = (rec["article"], rec["audio"])
        if pair in seen:
            continue
        seen.add(pair)

        audio = rec["audio"]
        article = rec["article"]
        has_tag = bool(re.search(r"(?i)(article|spoken|wiki|^en-)", audio))
        has_title = article.replace(" ", "_").lower() in audio.lower()
        in_whitelist = (audio in whitelist_set) or (normalize_wiki_title(audio) in whitelist_set)

        if not (has_tag or has_title or in_whitelist):
            continue

        by_article.setdefault(article, []).append(rec)

    all_articles = sorted(by_article.keys(), key=lambda s: s.lower())
    if ARTICLE_LIMIT and ARTICLE_LIMIT > 0:
        # Take a small buffer (limit + 5) before size/metadata checks so we still hit exact ARTICLE_LIMIT
        buffer_count = min(len(all_articles), ARTICLE_LIMIT + 5)
        if ARTICLE_SELECTION_MODE.lower() == "random":
            chosen = random.sample(all_articles, buffer_count)
        else:
            chosen = all_articles[:buffer_count]
    else:
        chosen = all_articles

    selected_records = []
    for art in chosen:
        selected_records.extend(by_article[art])
    return selected_records


def batch_fetch_file_metadata(filenames, session):
    metadata_map = {}
    chunk_size = 50
    for i in range(0, len(filenames), chunk_size):
        chunk = filenames[i : i + chunk_size]
        titles = "|".join([f"File:{f.replace(' ', '_')}" for f in chunk])
        url = "https://en.wikipedia.org/w/api.php"
        payload = {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "timestamp|user|url|size|sha1|extmetadata",
            "format": "json",
            "formatversion": "2",
        }
        try:
            res = session.post(url, data=payload, timeout=25)
            if res.status_code == 200:
                pages = res.json().get("query", {}).get("pages", [])
                for p in pages:
                    fname = p.get("title", "").replace("File:", "").replace(" ", "_")
                    media_info = p.get("imageinfo")
                    if media_info:
                        metadata_map[fname] = media_info[0]
        except requests.RequestException as exc:
            print(f"Warning: metadata batch request failed: {exc}", file=sys.stderr)
    return metadata_map


def deduplicate_and_limit_articles(candidate_records, metadata_map, whitelist_set):
    grouped = {}
    article_page_meta = {}

    for rec in candidate_records:
        article = rec["article"]
        audio = rec["audio"]
        if audio not in metadata_map:
            continue
        info = metadata_map[audio]

        in_whitelist = (audio in whitelist_set) or (normalize_wiki_title(audio) in whitelist_set)
        try:
            size = int(info.get("size", 0))
        except (ValueError, TypeError):
            size = 0

        if not in_whitelist and size < MIN_FILE_SIZE_BYTES:
            continue

        article_page_meta[article] = (rec["page_id"], rec["page_latest"])
        grouped.setdefault(article, []).append((audio, info))

    ordered_articles = list(grouped.keys())
    if ARTICLE_SELECTION_MODE.lower() != "random":
        ordered_articles.sort(key=lambda s: s.lower())

    final_items = []
    articles_kept = 0
    for article in ordered_articles:
        if ARTICLE_LIMIT and ARTICLE_LIMIT > 0 and articles_kept >= ARTICLE_LIMIT:
            break

        files = grouped[article]
        versions = {}
        for audio_file, info in files:
            ts = 0
            date_str = "1970-01-01"
            api_ts = info.get("timestamp", "")
            if api_ts:
                try:
                    dt = parse(api_ts, default=datetime(1970, 1, 1, tzinfo=timezone.utc))
                    ts = dt.timestamp()
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

            user = info.get("user", "Unknown")
            v_key = (user, date_str)
            if v_key not in versions:
                versions[v_key] = {"files": [], "total_size": 0, "max_ts": 0}
            versions[v_key]["files"].append((audio_file, info))
            try:
                versions[v_key]["total_size"] += int(info.get("size", 0))
            except (ValueError, TypeError):
                pass
            versions[v_key]["max_ts"] = max(versions[v_key]["max_ts"], ts)

        sorted_versions = sorted(versions.values(), key=lambda v: v["max_ts"], reverse=True)
        if not sorted_versions:
            continue

        selected_version = sorted_versions[0]
        for older_version in sorted_versions[1:]:
            if older_version["total_size"] > (selected_version["total_size"] * 1.3):
                selected_version = older_version
                break

        sorted_files = sorted(selected_version["files"], key=lambda x: x[0].lower())
        total_parts = len(sorted_files)
        page_id, page_latest = article_page_meta[article]
        for idx, (audio_file, info) in enumerate(sorted_files, start=1):
            final_items.append(
                {
                    "page_id": page_id,
                    "page_latest": page_latest,
                    "article": article,
                    "audio_file": audio_file,
                    "file_info": info,
                    "part_idx": idx,
                    "total_parts": total_parts,
                }
            )
        articles_kept += 1

    return final_items


def batch_fetch_wikitext(titles, api_url, session):
    wikitext_map = {}
    chunk_size = 50
    for i in range(0, len(titles), chunk_size):
        chunk = titles[i : i + chunk_size]
        payload = {
            "action": "query",
            "titles": "|".join(chunk),
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "formatversion": "2",
        }
        try:
            res = session.post(api_url, data=payload, timeout=25)
            if res.status_code == 200:
                for p in res.json().get("query", {}).get("pages", []):
                    if p.get("missing"):
                        continue
                    title_norm = p.get("title", "").replace(" ", "_")
                    revs = p.get("revisions", [])
                    if revs:
                        content = revs[0].get("slots", {}).get("main", {}).get("content", "")
                        wikitext_map[title_norm] = content
        except requests.RequestException as exc:
            print(f"Warning: wikitext batch failed ({api_url}): {exc}", file=sys.stderr)
    return wikitext_map


def fetch_earliest_upload_timestamps(filenames):
    earliest_map = {}
    if not filenames:
        return earliest_map

    def query_replica(host, dbname, targets):
        if not targets:
            return
        try:
            conn = get_db_connection(host, dbname)
        except Exception as exc:
            print(f"Warning: could not connect to {dbname} for upload history: {exc}", file=sys.stderr)
            return

        try:
            chunk_size = 200
            with conn.cursor() as cur:
                for i in range(0, len(targets), chunk_size):
                    chunk = targets[i : i + chunk_size]
                    placeholders = ",".join(["%s"] * len(chunk))
                    sql = f"""
                    SELECT
                        CONVERT(img_name USING utf8mb4) AS fname,
                        CONVERT(img_timestamp USING utf8mb4) AS img_ts,
                        (SELECT CONVERT(MIN(oi_timestamp) USING utf8mb4)
                         FROM oldimage
                         WHERE oi_name = img_name) AS min_oi_ts
                    FROM image
                    WHERE img_name IN ({placeholders});
                    """
                    cur.execute(sql, chunk)
                    for row in cur.fetchall():
                        fname = to_str(row["fname"])
                        candidates = [
                            to_str(row["img_ts"]),
                            to_str(row["min_oi_ts"]),
                        ]
                        valid = [c for c in candidates if len(c) == 14 and c.isdigit()]
                        if valid:
                            best = min(valid)
                            if fname not in earliest_map or best < earliest_map[fname]:
                                earliest_map[fname] = best
        except Exception as exc:
            print(f"Warning: upload timestamp query on {dbname} failed: {exc}", file=sys.stderr)
        finally:
            conn.close()

    query_replica(COMMONS_DB_HOST, COMMONS_DB_NAME, filenames)
    missing = [f for f in filenames if f not in earliest_map]
    if missing:
        query_replica(ENWIKI_DB_HOST, ENWIKI_DB_NAME, missing)
    return earliest_map


OLDID_PATTERNS = [
    re.compile(r"[?&]oldid=(\d{4,12})", re.IGNORECASE),
    re.compile(r"Special:(?:PermanentLink|Permalink)/(\d{4,12})", re.IGNORECASE),
    re.compile(r"\|\s*oldid\s*=\s*(\d{4,12})", re.IGNORECASE),
    re.compile(r"\boldid[_\s:=]+(\d{4,12})\b", re.IGNORECASE),
]


def extract_candidate_oldids(*texts):
    found = []
    seen = set()
    for text in texts:
        if not text:
            continue
        decoded = html.unescape(text)
        for pat in OLDID_PATTERNS:
            for match in pat.findall(decoded):
                oid = int(match)
                if oid not in seen:
                    seen.add(oid)
                    found.append(oid)
    return found


def extract_spoken_template_date(article_wikitext):
    if not article_wikitext:
        return None
    # Match {{Spoken Wikipedia ...}} block
    m = re.search(r"\{\{\s*Spoken[_\s]+Wikipedia\b([^}]*(?:\{\{[^}]*\}\}[^}]*)*)\}\}", article_wikitext, re.IGNORECASE)
    if not m:
        return None
    body = m.group(1)

    # Check {{Start date|YYYY|MM|DD}} inside template
    sd = re.search(r"\{\{\s*Start[_\s]+date[^|]*\|\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})", body, re.IGNORECASE)
    if sd:
        try:
            return datetime(int(sd.group(1)), int(sd.group(2)), int(sd.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass

    # Check explicit |date=... or positional YYYY-MM-DD
    dm = re.search(r"\|\s*date\s*=\s*([^|}\n]+)", body, re.IGNORECASE)
    raw_date = dm.group(1).strip() if dm else None
    if not raw_date:
        iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", body)
        raw_date = iso.group(1) if iso else None

    if raw_date:
        raw_date = re.sub(r"\{\{.*?\}\}", "", raw_date).strip()
        try:
            dt = parse(raw_date, default=datetime(2000, 1, 1, tzinfo=timezone.utc))
            if 2001 <= dt.year <= datetime.now(timezone.utc).year + 1:
                return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def extract_extmetadata_date(file_info):
    ext = file_info.get("extmetadata", {})
    for key in ("DateTimeOriginal", "DateTime"):
        raw = ext.get(key, {}).get("value", "")
        if raw:
            clean = re.sub(r"<[^<]+?>", "", raw).strip()
            clean = re.sub(r"\(.*?\)", "", clean).strip()
            try:
                dt = parse(clean, default=datetime(2000, 1, 1, tzinfo=timezone.utc))
                if 2001 <= dt.year <= datetime.now(timezone.utc).year + 1:
                    return dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, OverflowError):
                continue
    return None


def mw_ts_to_iso_date(mw_ts):
    if mw_ts and len(mw_ts) >= 8 and mw_ts[:8].isdigit():
        return f"{mw_ts[0:4]}-{mw_ts[4:6]}-{mw_ts[6:8]}"
    return "Unknown"


def resolve_revision_and_drift_metrics(items, article_wikitext_map, file_wikitext_map, earliest_upload_map):
    conn = get_db_connection(ENWIKI_DB_HOST, ENWIKI_DB_NAME)
    try:
        with conn.cursor() as cur:
            for item in items:
                page_id = item["page_id"]
                page_latest = item["page_latest"]
                article = item["article"]
                audio_file = item["audio_file"]
                info = item["file_info"]
                ext = info.get("extmetadata", {})

                desc_html = ext.get("ImageDescription", {}).get("value", "")
                credit_html = ext.get("Credit", {}).get("value", "")
                file_wt = file_wikitext_map.get(f"File:{audio_file}", "")
                art_wt = article_wikitext_map.get(article, "")

                candidate_oldids = extract_candidate_oldids(desc_html, credit_html, file_wt, audio_file)

                base_rev = None
                baseline_source = "upload_date"
                recording_date_display = None

                # 1. Primary: Permalink oldid from recording metadata
                for oid in candidate_oldids:
                    cur.execute(
                        """
                        SELECT rev_id, rev_page,
                               CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                               COALESCE(rev_len, 0) AS rev_len
                        FROM revision
                        WHERE rev_id = %s;
                        """,
                        (oid,),
                    )
                    row = cur.fetchone()
                    if row:
                        if int(row["rev_page"]) == page_id:
                            base_rev = row
                            baseline_source = "permalink"
                            recording_date_display = mw_ts_to_iso_date(to_str(row["rev_ts"]))
                            break
                        else:
                            # Oldid belonged to pre-merge history; anchor by its timestamp on current page_id
                            old_ts = to_str(row["rev_ts"])
                            cur.execute(
                                """
                                SELECT rev_id, rev_page,
                                       CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                                       COALESCE(rev_len, 0) AS rev_len
                                FROM revision
                                WHERE rev_page = %s AND rev_timestamp <= %s
                                ORDER BY rev_timestamp DESC, rev_id DESC
                                LIMIT 1;
                                """,
                                (page_id, old_ts),
                            )
                            alt = cur.fetchone()
                            if alt:
                                base_rev = alt
                                baseline_source = "permalink"
                                recording_date_display = mw_ts_to_iso_date(old_ts)
                                break

                # 2. Fallback 1: Recording date in {{Spoken Wikipedia}} or extmetadata DateTimeOriginal
                if not base_rev:
                    tpl_dt = extract_spoken_template_date(art_wt) or extract_extmetadata_date(info)
                    if tpl_dt:
                        cutoff_ts = tpl_dt.strftime("%Y%m%d235959")
                        cur.execute(
                            """
                            SELECT rev_id, rev_page,
                                   CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                                   COALESCE(rev_len, 0) AS rev_len
                            FROM revision
                            WHERE rev_page = %s AND rev_timestamp <= %s
                            ORDER BY rev_timestamp DESC, rev_id DESC
                            LIMIT 1;
                            """,
                            (page_id, cutoff_ts),
                        )
                        row = cur.fetchone()
                        if row:
                            base_rev = row
                            baseline_source = "metadata_date"
                            recording_date_display = tpl_dt.strftime("%Y-%m-%d")

                # 3. Fallback 2: Earliest upload timestamp of the recording file
                earliest_ts = earliest_upload_map.get(audio_file)
                if not earliest_ts:
                    api_ts = info.get("timestamp", "")
                    if api_ts:
                        try:
                            dt = parse(api_ts, default=datetime(2005, 1, 1, tzinfo=timezone.utc))
                            earliest_ts = dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
                        except Exception:
                            earliest_ts = "20050101000000"
                    else:
                        earliest_ts = "20050101000000"

                if not base_rev:
                    cur.execute(
                        """
                        SELECT rev_id, rev_page,
                               CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                               COALESCE(rev_len, 0) AS rev_len
                        FROM revision
                        WHERE rev_page = %s AND rev_timestamp <= %s
                        ORDER BY rev_timestamp DESC, rev_id DESC
                        LIMIT 1;
                        """,
                        (page_id, earliest_ts),
                    )
                    row = cur.fetchone()
                    if not row:
                        cur.execute(
                            """
                            SELECT rev_id, rev_page,
                                   CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                                   COALESCE(rev_len, 0) AS rev_len
                            FROM revision
                            WHERE rev_page = %s
                            ORDER BY rev_timestamp ASC, rev_id ASC
                            LIMIT 1;
                            """,
                            (page_id,),
                        )
                        row = cur.fetchone()
                    base_rev = row
                    baseline_source = "upload_date"
                    recording_date_display = mw_ts_to_iso_date(earliest_ts)

                # Prefer explicit recording date in display if available even when permalink resolved revision
                tpl_or_meta_dt = extract_spoken_template_date(art_wt) or extract_extmetadata_date(info)
                if tpl_or_meta_dt and baseline_source != "permalink":
                    recording_date_display = tpl_or_meta_dt.strftime("%Y-%m-%d")

                # Fetch current revision length & edit count since baseline
                cur.execute(
                    """
                    SELECT rev_id,
                           CONVERT(rev_timestamp USING utf8mb4) AS rev_ts,
                           COALESCE(rev_len, 0) AS rev_len
                    FROM revision
                    WHERE rev_id = %s;
                    """,
                    (page_latest,),
                )
                curr_rev = cur.fetchone()

                if base_rev and curr_rev:
                    base_rev_id = int(base_rev["rev_id"])
                    base_ts = to_str(base_rev["rev_ts"])
                    base_bytes = int(base_rev["rev_len"])
                    curr_bytes = int(curr_rev["rev_len"])

                    cur.execute(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM revision
                        WHERE rev_page = %s AND rev_timestamp > %s;
                        """,
                        (page_id, base_ts),
                    )
                    edits_row = cur.fetchone()
                    edits_since = int(edits_row["cnt"]) if edits_row else 0

                    delta_bytes = curr_bytes - base_bytes
                    abs_kb = abs(delta_bytes) / 1024.0
                    net_kb = delta_bytes / 1024.0
                    pct_kb = (delta_bytes / base_bytes * 100.0) if base_bytes > 0 else 0.0
                else:
                    base_rev_id = page_latest
                    edits_since = 0
                    abs_kb = 0.0
                    net_kb = 0.0
                    pct_kb = 0.0

                item["baseline_rev_id"] = base_rev_id
                item["baseline_source"] = baseline_source
                item["recording_date"] = recording_date_display or "Unknown"
                item["edits_since"] = edits_since
                item["abs_kb_changed"] = abs_kb
                item["net_kb_changed"] = net_kb
                item["pct_kb_changed"] = pct_kb
    finally:
        conn.close()


def extract_spoken_prose_tokens(parsoid_html):
    soup = BeautifulSoup(parsoid_html, "html.parser")

    for media_tag in soup.find_all(["audio", "video"]):
        media_tag.decompose()

    for file_node in soup.find_all(attrs={"typeof": re.compile(r"mw:File")}):
        file_node.decompose()

    destroy_classes = [
        "IPA", "noprint", "reference", "reflist", "refbegin",
        "hatnote", "metadata", "ambox", "mbox", "ombox", "dmbox", "fmbox",
        "mwe-math-element", "mw-highlight", "toc", "rellink", "excerpt-hat",
        "navbox", "navbar", "wgCoordinates", "mw-jump-link",
        "infobox", "gallery", "sidebar", "tsingle", "trow", "timeline-wrapper",
        "side-box-flex", "box-Expand_section", "geobox", "mod-gallery",
    ]
    for cls in destroy_classes:
        for el in soup.find_all(class_=cls):
            el.decompose()

    for el_id in ("catlinks", "footer", "siteSub", "header"):
        el = soup.find(id=el_id)
        if el:
            el.decompose()

    sections_to_remove = {"see also", "notes", "references", "external links", "further reading"}
    for h2 in soup.find_all("h2"):
        heading_text = h2.get_text().strip().lower()
        if heading_text in sections_to_remove:
            parent_sec = h2.find_parent("section")
            if parent_sec:
                parent_sec.decompose()
            else:
                curr = h2.find_next_sibling()
                h2.decompose()
                while curr and curr.name not in ("h1", "h2"):
                    nxt = curr.find_next_sibling()
                    curr.decompose()
                    curr = nxt

    for table in soup.find_all("table"):
        if len(table.find_all("tr")) >= 5:
            table.decompose()

    body = soup.find("body") or soup
    text = body.get_text(separator=" ", strip=True).lower()
    return re.findall(r"\b\w+\b", text)


def compute_prose_similarities(items, session, cache_conn):
    seen_pairs = {}
    cur = cache_conn.cursor()

    for item in items:
        if not ENABLE_PROSE_SIMILARITY:
            item["prose_similarity"] = None
            continue

        page_id = item["page_id"]
        base_rev = item["baseline_rev_id"]
        curr_rev = item["page_latest"]
        article = item["article"]

        if base_rev == curr_rev:
            item["prose_similarity"] = 100.0
            continue

        pair_key = (page_id, base_rev, curr_rev)
        if pair_key in seen_pairs:
            item["prose_similarity"] = seen_pairs[pair_key]
            continue

        cur.execute(
            "SELECT similarity FROM prose_sim_cache WHERE page_id = ? AND base_rev_id = ? AND curr_rev_id = ?",
            pair_key,
        )
        cached = cur.fetchone()
        if cached is not None:
            sim_val = float(cached[0])
            seen_pairs[pair_key] = sim_val
            item["prose_similarity"] = sim_val
            continue

        quoted_title = urllib.parse.quote(article, safe="")
        base_url = f"https://en.wikipedia.org/api/rest_v1/page/html/{quoted_title}/{base_rev}"
        curr_url = f"https://en.wikipedia.org/api/rest_v1/page/html/{quoted_title}/{curr_rev}"

        sim_val = None
        try:
            r_base = session.get(base_url, timeout=25)
            r_curr = session.get(curr_url, timeout=25)
            if r_base.status_code == 200 and r_curr.status_code == 200:
                base_tokens = extract_spoken_prose_tokens(r_base.text)
                curr_tokens = extract_spoken_prose_tokens(r_curr.text)
                if base_tokens or curr_tokens:
                    sim_val = round(difflib.SequenceMatcher(None, base_tokens, curr_tokens).ratio() * 100.0, 1)
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO prose_sim_cache
                        (page_id, base_rev_id, curr_rev_id, similarity, computed_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (page_id, base_rev, curr_rev, sim_val, datetime.now(timezone.utc).isoformat()),
                    )
                    cache_conn.commit()
        except Exception as exc:
            print(f"Warning: prose similarity failed for {article}: {exc}", file=sys.stderr)

        seen_pairs[pair_key] = sim_val
        item["prose_similarity"] = sim_val


def resolve_ffmpeg_binary():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    home_bin = os.path.expanduser("~/bin/ffmpeg")
    if os.path.isfile(home_bin) and os.access(home_bin, os.X_OK):
        return home_bin
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError(
            "No ffmpeg executable found. Ensure 'imageio-ffmpeg' is installed via pip or ffmpeg is in PATH."
        ) from exc


def parse_ebur128_stderr(stderr_text):
    # Extract from the final Summary: block emitted by ffmpeg's ebur128 filter
    summary_idx = stderr_text.rfind("Summary:")
    target = stderr_text[summary_idx:] if summary_idx != -1 else stderr_text

    m_i = re.search(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", target)
    m_lra = re.search(r"\bLRA:\s*(-?\d+(?:\.\d+)?)\s*LU\b", target)
    m_peak = re.search(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", target)

    lufs = float(m_i.group(1)) if m_i else None
    lra = float(m_lra.group(1)) if m_lra else None
    peak = float(m_peak.group(1)) if m_peak else None
    return lufs, lra, peak


def run_ffmpeg_lufs_stream(audio_url, ffmpeg_bin, session):
    af_opts = "ebur128=peak=true:framelog=quiet"
    if LUFS_DUAL_MONO:
        af_opts += ":dualmono=true"

    cmd = [
        ffmpeg_bin,
        "-nostats",
        "-hide_banner",
        "-i", "pipe:0",
        "-af", af_opts,
        "-f", "null",
        "-",
    ]

    stderr_chunks = []

    def drain_stderr(pipe):
        try:
            for line in iter(pipe.readline, b""):
                stderr_chunks.append(line.decode("utf-8", errors="replace"))
                if len(stderr_chunks) > 200:
                    stderr_chunks.pop(0)
        finally:
            pipe.close()

    start_time = time.monotonic()
    with session.get(audio_url, stream=True, timeout=(15, 60)) as resp:
        resp.raise_for_status()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        reader = threading.Thread(target=drain_stderr, args=(proc.stderr,), daemon=True)
        reader.start()

        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if time.monotonic() - start_time > AUDIO_TIMEOUT_SECONDS:
                    proc.kill()
                    raise TimeoutError(f"Exceeded {AUDIO_TIMEOUT_SECONDS}s streaming audio")
                if chunk and proc.poll() is None:
                    try:
                        proc.stdin.write(chunk)
                    except BrokenPipeError:
                        break
        finally:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        proc.wait(timeout=30)
        reader.join(timeout=5)

    stderr_text = "".join(stderr_chunks)
    lufs, lra, peak = parse_ebur128_stderr(stderr_text)

    # Fallback to ephemeral /tmp file if non-seekable pipe demuxing failed on an unusual container
    if lufs is None:
        with tempfile.NamedTemporaryFile(suffix=os.path.splitext(audio_url)[1] or ".ogg", delete=False) as tf:
            tmp_path = tf.name
        try:
            with session.get(audio_url, stream=True, timeout=(15, 60)) as resp2:
                resp2.raise_for_status()
                with open(tmp_path, "wb") as out_f:
                    for chunk in resp2.iter_content(chunk_size=65536):
                        if time.monotonic() - start_time > AUDIO_TIMEOUT_SECONDS:
                            raise TimeoutError(f"Exceeded {AUDIO_TIMEOUT_SECONDS}s downloading fallback audio")
                        if chunk:
                            out_f.write(chunk)
            res = subprocess.run(
                [ffmpeg_bin, "-nostats", "-hide_banner", "-i", tmp_path, "-af", af_opts, "-f", "null", "-"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=max(30, int(AUDIO_TIMEOUT_SECONDS - (time.monotonic() - start_time))),
                check=False,
            )
            lufs, lra, peak = parse_ebur128_stderr(res.stderr.decode("utf-8", errors="replace"))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    return lufs, lra, peak


def measure_audio_loudness(items, session, cache_conn):
    if not ENABLE_AUDIO_LUFS:
        for item in items:
            item["lufs"] = None
            item["lra"] = None
            item["true_peak"] = None
        return

    ffmpeg_bin = resolve_ffmpeg_binary()
    print(f"Using ffmpeg binary: {ffmpeg_bin}")

    cur = cache_conn.cursor()
    dual_flag = 1 if LUFS_DUAL_MONO else 0

    total = len(items)
    for idx, item in enumerate(items, start=1):
        audio_file = item["audio_file"]
        info = item["file_info"]
        sha1 = info.get("sha1", "") or "unknown_sha1"
        audio_url = info.get("url", "")

        if not FORCE_REANALYZE_AUDIO and sha1 != "unknown_sha1":
            cur.execute(
                """
                SELECT lufs, lra, true_peak
                FROM audio_lufs_cache
                WHERE audio_file = ? AND sha1 = ? AND dual_mono = ?
                """,
                (audio_file, sha1, dual_flag),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                item["lufs"], item["lra"], item["true_peak"] = row[0], row[1], row[2]
                print(f"  [{idx}/{total}] Cache hit: {audio_file} ({item['lufs']:.1f} LUFS)")
                continue

        if not audio_url:
            item["lufs"] = item["lra"] = item["true_peak"] = None
            continue

        print(f"  [{idx}/{total}] Streaming & measuring LUFS: {audio_file} ...")
        try:
            lufs, lra, peak = run_ffmpeg_lufs_stream(audio_url, ffmpeg_bin, session)
            item["lufs"] = lufs
            item["lra"] = lra
            item["true_peak"] = peak
            if lufs is not None and sha1 != "unknown_sha1":
                cur.execute(
                    """
                    INSERT OR REPLACE INTO audio_lufs_cache
                    (audio_file, sha1, dual_mono, lufs, lra, true_peak, measured_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (audio_file, sha1, dual_flag, lufs, lra, peak, datetime.now(timezone.utc).isoformat()),
                )
                cache_conn.commit()
            print(
                f"           -> {lufs if lufs is not None else 'N/A'} LUFS "
                f"(Peak: {peak if peak is not None else 'N/A'} dBFS, LRA: {lra if lra is not None else 'N/A'} LU)"
            )
        except Exception as exc:
            print(f"  Warning: LUFS analysis failed for {audio_file}: {exc}", file=sys.stderr)
            item["lufs"] = item["lra"] = item["true_peak"] = None

        if AUDIO_STREAM_SLEEP_SECONDS > 0:
            time.sleep(AUDIO_STREAM_SLEEP_SECONDS)


def render_html_report(items, output_path):
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total_rows = len(items)
    quiet_count = 0
    clip_count = 0
    high_drift_count = 0
    inferred_baseline_count = 0

    source_labels = {
        "permalink": ("Permalink", "Exact revision ID extracted from recording metadata permalink"),
        "metadata_date": ("Template Date*", "Inferred from recording date in {{Spoken Wikipedia}} or file metadata"),
        "upload_date": ("Upload Date*", "Fallback: article revision active when audio was first uploaded"),
    }

    rows_html = []
    for item in items:
        article = item["article"]
        base_title = article.replace("_", " ")
        part_idx = item["part_idx"]
        total_parts = item["total_parts"]
        part_badge = (
            f' <span class="badge badge-neutral">Part {part_idx}/{total_parts}</span>'
            if total_parts > 1
            else ""
        )

        article_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(article)}"
        file_page_url = f"https://commons.wikimedia.org/wiki/File:{urllib.parse.quote(item['audio_file'])}"
        direct_audio_url = item["file_info"].get("url", file_page_url)
        diff_url = (
            f"https://en.wikipedia.org/w/index.php?title={urllib.parse.quote(article)}"
            f"&type=revision&diff={item['page_latest']}&oldid={item['baseline_rev_id']}"
        )
        spokenview_url = f"{SPOKENVIEW_BASE_URL}{urllib.parse.quote(article)}"

        rec_date = item["recording_date"]
        b_src = item["baseline_source"]
        src_short, src_tip = source_labels.get(b_src, ("Inferred*", "Fallback revision"))
        is_inferred = b_src != "permalink"
        if is_inferred:
            inferred_baseline_count += 1

        edits = item["edits_since"]
        abs_kb = item["abs_kb_changed"]
        net_kb = item["net_kb_changed"]
        pct_kb = item["pct_kb_changed"]
        sign_prefix = "+" if net_kb >= 0 else ""

        prose_sim = item.get("prose_similarity")
        lufs = item.get("lufs")
        lra = item.get("lra")
        peak = item.get("true_peak")

        is_quiet = lufs is not None and lufs < LUFS_TOO_QUIET_THRESHOLD
        is_loud = lufs is not None and lufs > LUFS_TOO_LOUD_THRESHOLD
        is_clipping = peak is not None and peak > TRUE_PEAK_CLIP_THRESHOLD

        if is_quiet:
            quiet_count += 1
        if is_clipping:
            clip_count += 1

        if prose_sim is not None:
            is_high_drift = prose_sim < PROSE_SIMILARITY_WARN_BELOW
        else:
            is_high_drift = abs_kb >= ABS_KB_WARN_ABOVE

        if is_high_drift:
            high_drift_count += 1

        # Prose cell
        if prose_sim is not None:
            prose_cls = "badge-warn" if prose_sim < PROSE_SIMILARITY_WARN_BELOW else "badge-ok"
            prose_sort = f"{prose_sim:.2f}"
            prose_cell = f'<span class="badge {prose_cls}">{prose_sim:.1f}%</span>'
        else:
            prose_sort = "-1"
            prose_cell = '<span class="muted">N/A</span>'

        # LUFS cell
        if lufs is not None:
            if is_quiet:
                lufs_cls = "badge-danger"
                lufs_tag = "Quiet"
            elif is_loud:
                lufs_cls = "badge-warn"
                lufs_tag = "Hot"
            else:
                lufs_cls = "badge-ok"
                lufs_tag = ""
            lufs_sort = f"{lufs:.2f}"
            tag_html = f" ({lufs_tag})" if lufs_tag else ""
            peak_str = f"{peak:.1f} dBFS" if peak is not None else "N/A"
            clip_warn = ' <span class="clip-flag" title="True peak near or above 0 dBFS">CLIP</span>' if is_clipping else ""
            lra_str = f"{lra:.1f} LU" if lra is not None else "N/A"
            lufs_cell = (
                f'<div><span class="badge {lufs_cls}">{lufs:.1f} LUFS{tag_html}</span>{clip_warn}</div>'
                f'<div class="submetric">Peak: {peak_str} · LRA: {lra_str}</div>'
            )
        else:
            lufs_sort = "-999"
            lufs_cell = '<span class="muted">N/A</span>'

        row_flags = []
        if is_quiet:
            row_flags.append("quiet")
        if is_clipping:
            row_flags.append("clip")
        if is_high_drift:
            row_flags.append("drift")
        if is_inferred:
            row_flags.append("inferred")
        if total_parts > 1:
            row_flags.append("multipart")

        prose_col_td = (
            f'<td data-sort="{prose_sort}" class="num">{prose_cell}</td>'
            if ENABLE_PROSE_SIMILARITY
            else ""
        )

        rows_html.append(
            f'<tr data-flags="{" ".join(row_flags)}">'
            f'  <td data-sort="{html.escape(base_title.lower())}">'
            f'    <a href="{html.escape(article_url)}" target="_blank" rel="noopener" class="article-link">{html.escape(base_title)}</a>{part_badge}'
            f"  </td>"
            f'  <td data-sort="{html.escape(rec_date)}">'
            f"    <div>{html.escape(rec_date)}</div>"
            f'    <div class="submetric" title="{html.escape(src_tip)} (oldid={item["baseline_rev_id"]})">{html.escape(src_short)}</div>'
            f"  </td>"
            f'  <td data-sort="{html.escape(item["audio_file"].lower())}" class="links-cell">'
            f'    <a href="{html.escape(direct_audio_url)}" target="_blank" rel="noopener" title="{html.escape(item["audio_file"])}">Audio</a>'
            f'    · <a href="{html.escape(file_page_url)}" target="_blank" rel="noopener">File page</a>'
            f'    · <a href="{html.escape(diff_url)}" target="_blank" rel="noopener">Diff</a>'
            f'    · <a href="{html.escape(spokenview_url)}" target="_blank" rel="noopener" title="Open clean reading script in SpokenView">Script</a>'
            f"  </td>"
            f'  <td data-sort="{edits}" class="num">{edits:,}</td>'
            f'  <td data-sort="{abs_kb:.3f}" class="num">'
            f"    <div><strong>{abs_kb:.1f} KB</strong></div>"
            f'    <div class="submetric">{sign_prefix}{net_kb:.1f} KB ({sign_prefix}{pct_kb:.0f}%)</div>'
            f"  </td>"
            f"  {prose_col_td}"
            f'  <td data-sort="{lufs_sort}" class="num">{lufs_cell}</td>'
            f"</tr>"
        )

    prose_th = (
        '<th data-col="prose_sim" data-type="num" class="num" '
        'title="Word-level similarity of spoken body prose between baseline revision and current article">'
        "Prose Match</th>"
        if ENABLE_PROSE_SIMILARITY
        else ""
    )

    limit_banner = (
        f'<span class="badge badge-warn">Dev Limit Active: {ARTICLE_LIMIT} articles ({ARTICLE_SELECTION_MODE})</span>'
        if ARTICLE_LIMIT and ARTICLE_LIMIT > 0
        else '<span class="badge badge-neutral">Full Dataset</span>'
    )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Spoken Wikipedia Quality Control Report</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card: #ffffff;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #64748b;
      --accent: #0369a1;
      --ok-bg: #dcfce7; --ok-fg: #166534;
      --warn-bg: #fef3c7; --warn-fg: #92400e;
      --danger-bg: #fee2e2; --danger-fg: #991b1b;
      --neutral-bg: #e2e8f0; --neutral-fg: #334155;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px;
      line-height: 1.45;
    }}
    .container {{ max-width: 1400px; margin: 0 auto; }}
    header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: flex-end;
      gap: 12px;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0 0 4px 0; font-size: 1.55rem; }}
    .meta {{ color: var(--muted); font-size: 0.9rem; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
    }}
    .card .label {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
    .card .val {{ font-size: 1.5rem; font-weight: 700; margin-top: 2px; }}
    .controls {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 14px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 14px;
    }}
    .filter-group {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    button.pill, button.action-btn {{
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      padding: 6px 11px;
      border-radius: 6px;
      font-size: 0.85rem;
      cursor: pointer;
    }}
    button.pill.active {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    input[type="search"] {{
      padding: 6px 10px;
      border: 1px solid var(--border);
      border-radius: 6px;
      min-width: 240px;
      font-size: 0.9rem;
    }}
    .table-wrap {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow-x: auto;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #e2e8f0; text-align: left; vertical-align: middle; }}
    th {{
      background: #f1f5f9;
      font-weight: 600;
      cursor: pointer;
      user-select: none;
      white-space: nowrap;
      position: sticky;
      top: 0;
    }}
    th:hover {{ background: #e2e8f0; }}
    th.sort-asc::after {{ content: " ▲"; font-size: 0.75em; color: var(--accent); }}
    th.sort-desc::after {{ content: " ▼"; font-size: 0.75em; color: var(--accent); }}
    tr:hover td {{ background: #f8fafc; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .submetric {{ font-size: 0.78rem; color: var(--muted); margin-top: 2px; }}
    .muted {{ color: var(--muted); }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .article-link {{ font-weight: 600; }}
    .links-cell {{ white-space: nowrap; font-size: 0.85rem; }}
    .badge {{
      display: inline-block;
      padding: 2px 7px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
    }}
    .badge-ok {{ background: var(--ok-bg); color: var(--ok-fg); }}
    .badge-warn {{ background: var(--warn-bg); color: var(--warn-fg); }}
    .badge-danger {{ background: var(--danger-bg); color: var(--danger-fg); }}
    .badge-neutral {{ background: var(--neutral-bg); color: var(--neutral-fg); }}
    .clip-flag {{
      display: inline-block;
      margin-left: 4px;
      padding: 1px 5px;
      border-radius: 4px;
      background: #7f1d1d;
      color: #fff;
      font-size: 0.7rem;
      font-weight: 700;
    }}
    footer {{ margin-top: 18px; font-size: 0.82rem; color: var(--muted); }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>Spoken Wikipedia Quality Control Report</h1>
        <div class="meta">
          Generated {html.escape(now_utc)} · v{html.escape(SCRIPT_VERSION)} · {limit_banner}
        </div>
      </div>
      <div class="meta">
        Quiet threshold: < {LUFS_TOO_QUIET_THRESHOLD:.1f} LUFS ·
        Source: <a href="https://github.com/xenotropic/spokenreport" target="_blank" rel="noopener">GitHub</a>
      </div>
    </header>

    <div class="cards">
      <div class="card"><div class="label">Recordings Listed</div><div class="val" id="visible-count">{total_rows}</div></div>
      <div class="card"><div class="label">Quiet Audio (< {LUFS_TOO_QUIET_THRESHOLD:.0f} LUFS)</div><div class="val">{quiet_count}</div></div>
      <div class="card"><div class="label">Clipping (> {TRUE_PEAK_CLIP_THRESHOLD:.1f} dBFS)</div><div class="val">{clip_count}</div></div>
      <div class="card"><div class="label">High Text Drift</div><div class="val">{high_drift_count}</div></div>
      <div class="card"><div class="label">Missing Permalink*</div><div class="val">{inferred_baseline_count}</div></div>
    </div>

    <div class="controls">
      <div class="filter-group" id="filters">
        <button class="pill active" data-filter="all">All ({total_rows})</button>
        <button class="pill" data-filter="quiet">Quiet Audio ({quiet_count})</button>
        <button class="pill" data-filter="clip">Clipping ({clip_count})</button>
        <button class="pill" data-filter="drift">High Drift ({high_drift_count})</button>
        <button class="pill" data-filter="inferred">No Permalink* ({inferred_baseline_count})</button>
      </div>
      <div style="display:flex; gap:8px; align-items:center;">
        <input type="search" id="search-box" placeholder="Filter by article name or date...">
        <button class="action-btn" id="export-csv">Export CSV</button>
      </div>
    </div>

    <div class="table-wrap">
      <table id="qc-table">
        <thead>
          <tr>
            <th data-col="article" data-type="str">Article</th>
            <th data-col="date" data-type="str">Recorded Date</th>
            <th data-col="links" data-type="str">Links</th>
            <th data-col="edits" data-type="num" class="num">Edits Since</th>
            <th data-col="abs_kb" data-type="num" class="num">|Δ KB| (Net)</th>
            {prose_th}
            <th data-col="lufs" data-type="num" class="num">Loudness (EBU R128)</th>
          </tr>
        </thead>
        <tbody>
          {"".join(rows_html)}
        </tbody>
      </table>
    </div>

    <footer>
      * Rows marked <em>Template Date*</em> or <em>Upload Date*</em> lacked an explicit <code>oldid</code> permalink in file metadata and fell back to the article revision active on that date.
    </footer>
  </div>

  <script>
    (function() {{
      const table = document.getElementById('qc-table');
      const tbody = table.querySelector('tbody');
      const headers = Array.from(table.querySelectorAll('thead th'));
      const searchBox = document.getElementById('search-box');
      const visibleCountEl = document.getElementById('visible-count');
      let activeFilter = 'all';
      let currentColIdx = 0;
      let currentDir = '{DEFAULT_SORT_DIRECTION}';

      function sortByColumn(colIdx, dir) {{
        const th = headers[colIdx];
        if (!th) return;
        const isNum = th.dataset.type === 'num';
        const rows = Array.from(tbody.querySelectorAll('tr'));

        rows.sort((a, b) => {{
          const rawA = a.children[colIdx].dataset.sort || '';
          const rawB = b.children[colIdx].dataset.sort || '';
          let cmp = 0;
          if (isNum) {{
            const numA = parseFloat(rawA);
            const numB = parseFloat(rawB);
            cmp = numA - numB;
          }} else {{
            cmp = rawA.localeCompare(rawB);
          }}
          return dir === 'asc' ? cmp : -cmp;
        }});

        headers.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
        th.classList.add(dir === 'asc' ? 'sort-asc' : 'sort-desc');
        rows.forEach(r => tbody.appendChild(r));
        currentColIdx = colIdx;
        currentDir = dir;
      }}

      function applyFilters() {{
        const q = searchBox.value.trim().toLowerCase();
        let shown = 0;
        Array.from(tbody.querySelectorAll('tr')).forEach(row => {{
          const flags = (row.dataset.flags || '').split(' ');
          const flagOk = activeFilter === 'all' || flags.includes(activeFilter);
          const textOk = !q || row.textContent.toLowerCase().includes(q);
          const show = flagOk && textOk;
          row.style.display = show ? '' : 'none';
          if (show) shown++;
        }});
        visibleCountEl.textContent = shown;
      }}

      headers.forEach((th, idx) => {{
        th.addEventListener('click', () => {{
          const nextDir = (currentColIdx === idx && currentDir === 'desc') ? 'asc' : 'desc';
          sortByColumn(idx, nextDir);
        }});
      }});

      document.querySelectorAll('#filters .pill').forEach(btn => {{
        btn.addEventListener('click', () => {{
          document.querySelectorAll('#filters .pill').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          activeFilter = btn.dataset.filter;
          applyFilters();
        }});
      }});

      searchBox.addEventListener('input', applyFilters);

      document.getElementById('export-csv').addEventListener('click', () => {{
        const lines = [];
        lines.push(headers.map(h => '"' + h.textContent.trim().replace(/"/g, '""') + '"').join(','));
        Array.from(tbody.querySelectorAll('tr')).forEach(row => {{
          if (row.style.display === 'none') return;
          const cols = Array.from(row.children).map(td => {{
            const txt = td.textContent.replace(/\\s+/g, ' ').trim();
            return '"' + txt.replace(/"/g, '""') + '"';
          }});
          lines.push(cols.join(','));
        }});
        const blob = new Blob([lines.join('\\n')], {{ type: 'text/csv;charset=utf-8;' }});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'spoken_wikipedia_qc_report.csv';
        a.click();
      }});

      const defaultIdx = headers.findIndex(h => h.dataset.col === '{DEFAULT_SORT_COLUMN}');
      sortByColumn(defaultIdx >= 0 ? defaultIdx : 0, '{DEFAULT_SORT_DIRECTION}');
    }})();
  </script>
</body>
</html>
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=os.path.dirname(output_path)
    ) as tf:
        tf.write(html_doc)
        tmp_name = tf.name
    os.replace(tmp_name, output_path)
    os.chmod(output_path, 0o644)
    print(f"Report written to {output_path} ({total_rows} recording rows).")


def main():
    print(f"=== Spoken Wikipedia QC Report v{SCRIPT_VERSION} (Limit: {ARTICLE_LIMIT or 'ALL'}) ===")
    session = create_http_session()
    cache_conn = init_cache_db(CACHE_DB_PATH)

    try:
        print("1/6 Querying enwiki_p for Category:Spoken_articles media links...")
        raw_records = fetch_spoken_records()
        print(f"    Retrieved {len(raw_records)} raw media links.")

        print("2/6 Fetching Wikipedia:Spoken_articles whitelist & prefiltering...")
        whitelist_set = fetch_index_whitelist(session)
        candidates = prefilter_candidates(raw_records, whitelist_set)
        unique_candidate_files = sorted({r["audio"] for r in candidates})
        print(f"    Selected {len(unique_candidate_files)} candidate audio files to inspect.")

        print("3/6 Fetching MediaWiki imageinfo metadata...")
        metadata_map = batch_fetch_file_metadata(unique_candidate_files, session)
        items = deduplicate_and_limit_articles(candidates, metadata_map, whitelist_set)
        print(f"    Retained {len(items)} active spoken recording files.")

        print("4/6 Resolving baseline oldid revisions & edit/byte deltas...")
        unique_articles = sorted({it["article"] for it in items})
        article_wikitext_map = batch_fetch_wikitext(
            unique_articles, "https://en.wikipedia.org/w/api.php", session
        )

        # Fetch raw File: wikitext only for files whose extmetadata lacked an obvious oldid
        files_needing_wt = []
        for it in items:
            ext = it["file_info"].get("extmetadata", {})
            desc = ext.get("ImageDescription", {}).get("value", "")
            cred = ext.get("Credit", {}).get("value", "")
            if not extract_candidate_oldids(desc, cred, it["audio_file"]):
                files_needing_wt.append(f"File:{it['audio_file']}")

        file_wikitext_map = {}
        if files_needing_wt:
            file_wikitext_map.update(
                batch_fetch_wikitext(files_needing_wt, "https://commons.wikimedia.org/w/api.php", session)
            )
            still_missing = [f for f in files_needing_wt if f.replace(" ", "_") not in file_wikitext_map]
            if still_missing:
                file_wikitext_map.update(
                    batch_fetch_wikitext(still_missing, "https://en.wikipedia.org/w/api.php", session)
                )

        earliest_upload_map = fetch_earliest_upload_timestamps(
            sorted({it["audio_file"] for it in items})
        )
        resolve_revision_and_drift_metrics(
            items, article_wikitext_map, file_wikitext_map, earliest_upload_map
        )

        print("5/6 Computing spoken prose similarity...")
        compute_prose_similarities(items, session, cache_conn)

        print("6/6 Analyzing audio LUFS loudness...")
        measure_audio_loudness(items, session, cache_conn)

        render_html_report(items, OUTPUT_HTML_PATH)
    finally:
        cache_conn.close()


if __name__ == "__main__":
    main()