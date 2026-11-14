# insta360-card-recovery

Recover **deleted Insta360 recordings** (full-resolution `.insv` VIDs and their
`.lrv` previews) from an image of the SD card, and prove every recovered file
is right before it is handed over.

When a camera deletes files or the card is reformatted, the exFAT cluster
chains are gone but most of the video data is still on the card. Insta360
files are fragmented in a recognisable way: VID and LRV are written at the same
time and interleaved, and the first clusters of each recording are parked in
small free holes elsewhere on the card. This tool rebuilds each file's cluster
order from the video bitstream itself, decodes every frame to check the
result, compares it against the independently recovered preview, and only then
publishes it.

It was built for, and tested on, two Insta360 X5 cards (≈170 recordings, ≈8 h
of 2 × 3840×3840 HEVC 360° video): every recording that was not physically
overwritten came back bit-exact.

## What you get

For every recording it can verify:

* the original file, byte-for-byte as it was on the card (no re-encoding):
  `VID_<date>_<time>_00_<nnn>.insv`, `LRV_..._01_<nnn>.lrv`
* the Insta360 trailer (gyro/IMU, lens calibration, thumbnails) rebuilt so
  Insta360 Studio can stabilise and reframe it, when all its records survived
* a `MANIFEST.csv` row: size, duration, streams, decoded/expected frame counts,
  decode exit codes and error lines, how the picture was verified, SHA-256,
  and the cluster chain it came from

A file is published only when **all** of these hold:

1. its size and every stream match the camera's own sample tables;
2. a full FFmpeg decode of every stream exits 0 with **zero** error lines and
   decodes exactly the expected number of frames;
3. its picture matches the companion recording (VID vs LRV, at 8 points
   through the clip), or a person approved its contact sheet;
4. sampled clusters are byte-identical to the card image.

Nothing is guessed. A cluster that cannot be proven is left unresolved and the
file is not published.

## Requirements

* Python 3.10+ (standard library only)
* FFmpeg/ffprobe 6.x in **bash**: native on Linux/macOS; on Windows inside
  WSL (`wsl --install`, then `sudo apt install ffmpeg` in Ubuntu)
* A card image: an `.rdr` image, or a raw image (`dd`, `.img`) of the card.
  The work folder needs free space for the recovered files (roughly the size
  of the recordings) on a local disk; the image itself can stay on a NAS.
* Card layout: exFAT with **256 KiB clusters** (the usual for large SDXC
  cards). `python recovery/card_info.py <image>` checks this.

Never write to the card itself. Image it first, keep the image read-only.

## Quick start

```bash
cp recovery.example.json recovery.json       # then edit: work folder + image paths
python recovery/card_info.py card1           # check the card layout
python recovery/run_recovery.py --image card1
```

That runs every stage unattended and resumes where it left off if stopped.
Expect hours to days: the full decode check runs at a few times real time on
a laptop CPU (more cores = faster; a GPU does not help, hardware decoders hide
the errors this check exists to find).

Progress at any time:

```bash
python recovery/recovery_status.py
```

Anything the automatic picture check could not approve is listed in
`work/review/needs-eyes-<image>.txt`. To look at those:

```bash
python recovery/run_recovery.py --image card1 --stages review
# look at work/review/<image>-sheets-*.jpg, add good labels to
# work/review/approved-<image>.txt (one per line), then:
python recovery/run_recovery.py --image card1 --stages publish,inst,audit
```

## Stages

| stage | what it does |
|---|---|
| `index` | builds and caches the image's chunk index |
| `scan` | finds every MP4 file header and `moov` atom on the card |
| `catalog` | pairs headers with `moov` atoms: the list of recordings and their sample tables |
| `numbers` | camera file numbers and local recording times (names) |
| `chains` | walks each file's cluster chain, checking every predicted NAL boundary |
| `lrv` | assembles and fully decode-validates the previews |
| `blinds` | places "blind" clusters (inside one NAL, nothing to check) by trial-decoding every candidate against the preview |
| `lost` | continues broken chains step by step, confirming each weak step by decoding |
| `vid` | assembles and fully decode-validates the full-resolution files |
| `autoreview` | approves files whose VID and LRV agree picture-for-picture |
| `review` | contact sheets for the rest (manual) |
| `publish` | hard-links approved files into the outputs folder, writes the manifest |
| `inst` | rebuilds and installs the Insta360 trailers |
| `audit` | byte-compares sampled clusters of every published file with the image |

See [docs/METHOD.md](docs/METHOD.md) for how the chain reconstruction and the
checks work, and [docs/OPERATIONS.md](docs/OPERATIONS.md) for tuning and
troubleshooting.

## Limits

* Clusters that were overwritten after deletion are gone; a recording that
  lost one is not published (its preview often still is).
* Blind clusters and lost chains can only be proven for VIDs with a
  recovered LRV (the LRV is the reference picture).
* Tested on Insta360 X5 files. Other Insta360 models use the same container
  and should work; other cameras need their own layout rules.
