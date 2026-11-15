# Running and troubleshooting

## Configuration (`recovery.json`)

```json
{
  "work": "D:/insta360-recovery/work",
  "outputs": "D:/insta360-recovery/outputs/recovered",
  "images": {
    "card1": "//nas/share/card1.rdr",
    "card2": "D:/images/card2.img"
  },
  "utc_offset": {"card1": 1, "card2": -7},
  "flush_cache": true
}
```

* `work` must be a local disk (on Windows FFmpeg reaches it through WSL as
  `/mnt/<drive>/…`). Images may be on a network share.
* `utc_offset` is only the starting guess for file names; once trailers are
  rebuilt, each recording's own camera timestamp is used.
* Point `RECOVERY_CONFIG` at the file to keep it elsewhere.

## One card at a time

`run_recovery.py` takes `work/run.lock`; a second run on the same work folder
refuses to start (`--force-lock` if a crashed run left the lock behind).
Everything that writes `MANIFEST.csv` also takes a lock, so publishing and
trailer rebuilds never overwrite each other.

## Time and resources

* Full decode validation is the slow part: ~3–5× real time for 2 × 3840×3840
  HEVC on a 4-core laptop. More CPU cores help; a GPU does not.
* Fast blind trials: ~0.4 s per candidate; a typical gap (2,000–3,000
  candidates after the byte filter) takes 15–50 minutes.
* Disk: the assembled files need about the size of the recordings;
  `--min-free-gb` (default 50) stops assembly before the disk fills.

### WSL memory (Windows)

Decoding multi-GB files through `/mnt/c` fills the Linux VM's page cache. When
Windows runs short of RAM, reads inside WSL start failing with
`Cannot allocate memory` and decodes stop partway. The tool handles this:

* `flush_cache: true` runs `drop_cache_loop.sh` as root in WSL during the heavy
  stages (flushes clean cache every 15 s; no settings are changed);
* the validator re-runs a stream that died this way, and `run_recovery.py`
  re-validates any file still cut short.

A `.wslconfig` with more `memory=` also helps.

## Reading progress

`python recovery/recovery_status.py` puts every catalogued file in one state:

| state | meaning |
|---|---|
| published | in the outputs folder and the manifest |
| reviewed-ok | validated clean, waiting for approval / publish |
| decode-bad | validated, a stream failed (exit, error lines or frame count) |
| validating | assembled, validation pending or running |
| queued | chain complete, not assembled yet (or under 2 s, skipped) |
| blind | chain has unresolved blind clusters |
| lost | no complete chain |

## Diagnosing

| symptom | look at | likely cause |
|---|---|---|
| file stays `blind` | `work/trials/fast-<label>/k<K>-result.json` | right chunk overwritten, or another gap in the same frame still wrong |
| file stays `lost` | `work/logs/<image>-lost-*.log` | next cluster overwritten, or several blind clusters in a row (manual) |
| `decode-bad` with `oom_retries` | `work/validation/<label>/decode-*.oom*.log` | memory pressure (see above); the next run re-validates |
| `decode-bad` without | `work/validation/<label>/decode-*.log` | a wrong placement: rerun `blinds`, compare chain backups |
| auto-review sends a file to `needs-eyes` | `work/review/auto-<image>.json` | no recovered companion, or a real mismatch: look at the sheet |
| trailer `incomplete` | `work/inst/<label>.json` | some trailer records were overwritten; video/audio unaffected |

## Tools

| script | purpose |
|---|---|
| `run_recovery.py` | the whole pipeline, resumable |
| `card_info.py` | checks the exFAT layout of an image |
| `recovery_status.py` | one-screen progress report |
| `audit_published.py` | byte audit of published files against the image |
| `fast_trial.py` | trial-decode one blind cluster against every chunk in a window |
| `apply_decode.py` | apply fast-trial placements (several gaps jointly) |
| `blind_stage.py` | all blind gaps of an image |
| `rebuild_lost.py` / `resume_lost.py` / `lost_decode.py` / `probe_lost.py` | lost-chain reconstruction |
| `auto_review.py` / `review_sheets.py` | automatic and manual picture check |
| `chain_walk.py`, `batch_chains.py`, `retry_lost.py`, `resolve_blinds.py`, `trial_blinds.py` | chain reconstruction |
| `pipeline.py`, `assemble_chain.py`, `validate_media2.sh`, `make_sheet.sh` | assemble + validate |
| `deliver.py`, `deliver_batch.py` | gated publishing + manifest |
| `rebuild_inst.py`, `finalize_inst.py`, `inst_publish.py`, `set_local_time.py` | trailers and names |
| `rdr_reader.py`, `mp4_tables.py` | image reader and MP4 table parser |
| `probe_parallel.py` | `probe_lost.py` over several processes (card-wide searches) |
| `probe_anchor.py`, `find_reentry.py`, `patch_walk.py` | patching a file whose start was overwritten (below) |

## Patched files (start of a file overwritten)

When the card-wide search shows the clusters right after a file's header no longer exist (every
candidate for cluster 1 fails decode confirmation), the file cannot be recovered byte-exact. It can
still be **patched**. Patched files never go into `recovered/` or its manifest.

1. `find_reentry.py <chains> <label> <lo> <hi> <k>...`: for later clusters that hold a sample start,
   test every free chunk using only the moov's sample-start anchors (no NAL state carried across the
   gap), then follow each hit forward. A re-entry point needs at least 40 bits.
2. `patch_walk.py <chains> <label> <reenter_k> <reenter_chunk> --strict`: records the missing clusters
   as `missing-zero` and walks on from the re-entry with the normal walker. Writes
   `work/chains/<chains>-patched/<label>.json`.
3. `assemble_chain.py <that json> --out <somewhere outside assembled/> --allow-missing`: zeros for the
   missing clusters. Without the flag the assembler refuses.
4. `rebuild_inst.py <label> --chains <set> --zero-missing`: the trailer, with unrecoverable slots
   zero-filled. It writes `<label>.patched.inst.bin` / `<label>.patched-plan.json`, never the
   verified `<label>.inst.bin`, so `finalize_inst.py` cannot install it by accident. For `--chains`,
   use a set holding the verified chains plus the patched chain with its `missing-zero` entries dropped.
5. Validate as usual. Expect decode errors and a smeared picture up to the first keyframe after the
   gap. Inspect the frames around that keyframe. Then put the file in a separate folder with a
   README that states exactly what is missing.
