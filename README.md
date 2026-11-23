# Spoken Wikipedia Quality Control Report (`spokenreport`)

`spokenreport` generates a periodic static HTML report evaluating the state of English Wikipedia's [WikiProject Spoken Wikipedia](https://en.wikipedia.org/wiki/Wikipedia:WikiProject_Spoken_Wikipedia) audio recordings. Hosted on Wikimedia Toolforge at https://spokenreport.toolforge.org/, the tool helps editors identify recordings that have drifted substantially from the live article text or that exhibit technical audio issues such as low volume or digital clipping.

## Overview of How It Works

1. **Discovery:** Queries the English Wikipedia MariaDB replica (`enwiki_p`) for articles in `Category:Spoken_articles` and cross-references linked media files against `Wikipedia:Spoken_articles` and MediaWiki `imageinfo` metadata to isolate spoken-article recordings from incidental pronunciation or music clips. When multiple versions of an article exist, the most recent complete recording is selected; multi-part recordings are listed individually with part indicators.
2. **Baseline Revision Resolution:** Determines which historical revision (`oldid`) of the Wikipedia article was read aloud, using metadata permalinks where available and date-based fallbacks otherwise.
3. **Text Drift Calculation:** Queries revision history and byte lengths from `enwiki_p`, then fetches rendered HTML from the MediaWiki REST API for both the baseline revision and the current live revision to measure how closely the spoken prose still matches.
4. **Audio Loudness Analysis:** Streams each audio file through `ffmpeg`'s EBU R128 loudness filter (`ebur128`) without storing audio files on disk, caching measurements in a local SQLite database keyed by the file's `SHA-1` hash so unchanged files are only analyzed once.

---

## Report Columns Explained

### Article
Links directly to the live English Wikipedia article. Multi-part recordings include a `Part X/Y` badge next to the title.

### Recorded Date (and Baseline Source)
Displays the `YYYY-MM-DD` date associated with the recording, paired with a subtitle showing how the tool determined the baseline article revision (`oldid`) used for all change comparisons:

* **Permalink:** Extracted directly from an explicit `oldid` or `Special:PermanentLink` URL recorded in the file's Commons/Wikipedia metadata (`{{Spoken article}}` or `{{Information}}`). This is exact.
* **Template Date\*:** No `oldid` permalink was found in the file metadata. The tool fell back to the recording date specified in the `{{Spoken Wikipedia}}` template on the article (or `DateTimeOriginal` in the file metadata) and selected the last revision of the article saved on or before `23:59:59 UTC` on that date.
* **Upload Date\*:** Neither a permalink nor a template date was available. The tool queried `image` and `oldimage` upload logs on Wikimedia Commons and English Wikipedia for the earliest upload timestamp of the file and selected the article revision active at that moment.

Hovering over the source label in the report displays a tooltip containing the exact resolved `oldid`.

### Links
Four utility links for inspecting or updating the recording:
* **Audio:** Direct link to the raw audio stream (`upload.wikimedia.org`).
* **File page:** Link to the file description page on Wikimedia Commons (or English Wikipedia) to inspect licensing, narrator metadata, or upload a normalized/updated file.
* **Diff:** Opens a MediaWiki `Special:Diff` view comparing the resolved baseline revision directly against the current live revision of the article.
* **Script:** Opens the article in [SpokenView](https://spokenview.toolforge.org/), which strips non-spoken Wikipedia clutter into a clean read-aloud script for re-recording.

### Edits Since
The total number of revisions made to the Wikipedia article since the baseline revision timestamp. High edit counts indicate active maintenance, vandalism/reverts, or gnoming (such as category and citation formatting), though not necessarily changes to the spoken body text.

### |Δ KB| (Net)
Measures the change in raw wikitext size between the baseline revision and the current revision, where `1 KB = 1,024 bytes`:
* **Bold top value (`|Δ KB|`):** The absolute size difference, used for sorting so large deletions rank alongside large additions.
* **Subtitle (`+X.X KB (+Y%)`):** The signed net change in kilobytes and the percentage change relative to the article's original wikitext length.

#### Intuitive Scale of Wikitext Kilobytes
In English prose encoded as UTF-8, 1 character is typically 1 byte, and an average English word plus its trailing space is roughly 6 bytes. However, raw Wikipedia source (`rev_len`) includes templates, `<ref>` citations, infoboxes, tables, and categories, which often account for 30% to 50% of an article's byte count without being read aloud:
* **0.1 to 1.0 KB:** Roughly 15 to 150 words of plain text—equivalent to fixing several typos, adding a short paragraph, or inserting one or two `{{cite web}}` templates (a single full citation template is typically 0.3 to 0.5 KB).
* **5 to 10 KB:** Roughly 800 to 1,600 words of pure prose (about 5 to 11 minutes of spoken audio at 150 words per minute), or a mix of several new paragraphs and 10–15 inline references.
* **30 to 50 KB:** Equivalent to 5,000 to 8,000 words of raw text—roughly the length of an entire standalone Featured Article or a 30-to-50-minute recording.
* **100 KB:** Roughly 16,000 words (around 35–40 single-spaced printed pages), representing a massive expansion or complete overhaul.

Note that raw byte delta only compares total length before and after: if an editor deletes a 10 KB section and replaces it with a completely new 10 KB section, the net and absolute byte change is `0.0 KB` even though the text changed substantially. Conversely, converting bare URLs into verbose citation templates can add `15 KB` without changing a single spoken word. Use **Prose Match** alongside **|Δ KB|** to separate markup churn from actual spoken changes.

### Prose Match
Estimates the percentage of spoken prose shared between the recorded version of the article and the current live version (`100.0%` means the spoken words are identical; rows below `75.0%` are highlighted as **High Drift**).

#### How Prose Match Is Calculated
Rather than comparing raw wikitext, the script fetches rendered Parsoid HTML from the MediaWiki REST API for both the baseline `oldid` and the current revision, then strips away elements that a Spoken Wikipedia narrator does not read aloud:
* Infoboxes, navboxes, sidebars, maintenance banners (`ambox`), hatnotes, coordinates, IPA pronunciation spans, and galleries.
* Inline footnote markers (`[1]`, `[citation needed]`) and non-spoken trailing sections (*See also*, *Notes*, *References*, *Further reading*, and *External links*).
* Data tables containing five or more rows and embedded audio/video widgets.

The surviving body text and headings are lowercased, tokenized into sequential word lists, and compared using Python's `difflib.SequenceMatcher` (`2 * M / T`, where `M` is the number of matching words in order and `T` is the total number of words across both versions). Because markup and citations are removed first, adding 30 footnotes leaves **Prose Match** near `100%`, whereas rewriting paragraphs in place lowers the score even when **|Δ KB|** stays near `0.0 KB`.

### Loudness (EBU R128)
Displays three audio engineering measurements computed across the full duration of the recording via `ffmpeg`'s `ebur128` filter (implementing ITU-R BS.1770 / EBU R128):

#### 1. Integrated Loudness (`LUFS`) and Quiet/Hot Thresholds
**LUFS** (Loudness Units relative to Full Scale) measures perceived loudness over the entire file, weighted to match human hearing sensitivity across frequencies and gated so silent pauses between sentences do not drag down the average.
* Spoken-word podcasts and mobile streaming platforms generally target **-16 LUFS** (stereo) to **-19 LUFS** (mono). European broadcast television/radio (EBU R128) targets **-23 LUFS** (and US broadcast ATSC A/85 targets **-24 LKFS**), which is the quietest standard delivery level used in professional broadcasting.
* **Quiet (`< -23.0 LUFS`):** Recordings quieter than `-23.0 LUFS` fall below even conservative broadcast standards. On mobile devices, headphones, or car stereos—especially in environments with ambient background noise—listeners may struggle to hear the narration even with system volume turned up near maximum. Recordings at `-30 LUFS` or `-40 LUFS` are whisper-quiet and strong candidates for simple non-destructive loudness normalization.
* **Hot (`> -12.0 LUFS`):** Recordings louder than `-12.0 LUFS` are unusually aggressive for spoken word, often indicating heavy dynamic compression or excessive microphone gain.

*(Note: By default, mono recordings are measured as single-channel signals without dual-mono +3 LU compensation; a mono file normalized to -19 LUFS reads as -19 LUFS.)*

#### 2. True Peak (`Peak: X.X dBFS`) and the `CLIP` Indicator
Digital audio has a hard ceiling at `0.0 dBFS` (decibels relative to full scale); waveform crests that exceed `0.0` are squared off ("clipped"), producing audible crackle or harsh distortion on loud consonants and plosives.

Standard sample-peak meters only inspect the discrete digital snapshots stored in the file (e.g., 44,100 samples per second). However, when a listener's device converts those digital samples back into a continuous analog sound wave—or transcodes an `.ogg`/`.flac` file into `.mp3`—the reconstructed curve between two adjacent high samples routinely overshoots the stored sample values. These are called **inter-sample peaks**. Because `ebur128=peak=true` oversamples the audio to measure True Peak, a recording can legitimately display positive readings such as `+0.3 dBFS` or `+1.0 dBFS` when inter-sample crests exceed the digital ceiling.

* **`CLIP` badge (`Peak > -0.2 dBFS`):** Industry standards (such as EBU R128 and Apple Podcasts) recommend keeping True Peak at or below `-1.0 dBTP` to leave safe headroom for lossy codec transcoding. Because many volunteer recordings on Wikipedia are normalized right up to `-0.3` or `-0.5 dBFS` without audible harm, this report uses a stricter threshold of **`> -0.2 dBFS`** to flag files that actually hit `0.0 dBFS` or overshoot into positive inter-sample clipping. Note that a file can simultaneously be **Quiet** overall (e.g., `-25 LUFS`) and still trigger **`CLIP`** if a single cough, mic bump, or unguarded plosive spiked to `0.0 dBFS`.

#### 3. Loudness Range (`LRA: X.X LU`)
Measures dynamic variation in loudness (in Loudness Units) between softer and louder passages across the recording, excluding brief extremes. Consistent spoken narration typically sits between **3 LU and 8 LU**. Very high LRA values (e.g., `> 12 LU`) suggest uneven distance from the microphone, inconsistent volume stitched across multiple recording sessions, or loud intro/outro music paired with quiet speech.

---

## Configuration and Running

All tunable thresholds, database endpoints, output paths, and development limits are grouped in the `USER CONFIGURATION` block at the top of `generatereport.py`:

* `ARTICLE_LIMIT`: Number of articles to process (`10` or `50` for testing; `0` for all Spoken Wikipedia articles).
* `ARTICLE_SELECTION_MODE`: `"alphabetical"` or `"random"` when `ARTICLE_LIMIT > 0`.
* `ENABLE_AUDIO_LUFS` / `FORCE_REANALYZE_AUDIO`: Toggle `ffmpeg` loudness streaming or bypass the SQLite `SHA-1` cache.
* `ENABLE_PROSE_SIMILARITY`: Toggle Parsoid HTML fetching and word-level similarity scoring.
* `LUFS_TOO_QUIET_THRESHOLD`, `LUFS_TOO_LOUD_THRESHOLD`, `TRUE_PEAK_CLIP_THRESHOLD`, `PROSE_SIMILARITY_WARN_BELOW`: Customize alert badge thresholds.

### Running Manually on Toolforge

```bash
become spokenreport
toolforge webservice python3.13 shell
source ~/pyvenv/bin/activate
python3 ~/spokenreport/generatereport.py
exit
```
License
MIT License.
Source code and issue tracking: https://github.com/xenotropic/spokenreport

