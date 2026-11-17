# insta360-card-recovery

**I quick-formatted my Insta360 SD cards by accident. This is how I got my videos and photos back,
and the tool I built to do it.**

## What happened

I had two microSD cards from my Insta360 X5. One was from a ski trip in January, the other from a
summer trip the August before. Both were full of 360° video and photos that I hadn't copied anywhere
yet. Then I formatted them. It was a quick format, and I honestly can't remember whether I did it
on the PC or in the camera itself. Either way, I regretted it the second it was done.

The good news about a quick format: it doesn't erase your footage. It resets the card's
bookkeeping (the file table, the allocation bitmap and the folder listing). The card *looks*
empty, but the data is still there until something new is written over it. So:

1. **I stopped using the cards.** No recording, no "let me just check", nothing that writes.
2. **I made a full image of each card** and only ever worked on those images, never on the
   cards themselves.
3. **I tried the usual recovery tools.** They found file names and lots of data, but most of the
   "recovered" videos wouldn't open, or were full of glitches.

Here's why. The X5 doesn't write a video in one piece. It records the full-resolution video and a
low-res preview at the same time, interleaved on the card. It also tucks the beginning of each
file into small free gaps elsewhere on the card. Once the file table is gone, a normal recovery
tool assumes each file is one continuous block, and ends up gluing together pieces of different
recordings.

4. **So I built this tool, with a lot of help from AI coding agents.** It works out the true order
   of every 256 KB cluster from the data itself. For videos, the camera's own index at the end of
   each file says exactly how big every frame is, so every candidate cluster can be checked
   against where the next frame has to start. For photos, the JPEG data and two built-in copies of a
   preview image pin down every cluster. Then everything is proven: every video frame is decoded
   and compared with the preview, and every photo is decoded bit by bit.

## What I got back

| | card 1 (January) | card 2 (August) |
|---|---|---|
| recordings on the card | 96 | 76 |
| full-resolution videos, bit-exact and fully verified | **90** | **70** |
| patched (first 2 seconds damaged, the rest perfect) | 1 | 1 |
| only the low-res preview survived | | 3 |
| skipped: accidental clips under 2 seconds | 5 | 2 |
| 360° photos, bit-exact and verified | **19** | **22** |

That's about 8 hours of 360° video and 41 photos. The only things missing are pieces that had
already been overwritten, plus a few accidental sub-2-second clips I didn't bother with. The
recovered files are the camera's originals (nothing re-encoded). They open in Insta360 Studio
with stabilisation and reframing, because the tool also rebuilds the Insta360 metadata (gyro data,
lens calibration) that Studio needs.

## If this just happened to you

* **Stop using the card.** Don't record, don't format again, don't run "repair" tools on it.
* **Make an image of the whole card** ([how](docs/RUN-IT-YOURSELF.md#1-image-the-card)), then put
  the card in a drawer. It's your backup if anything goes wrong.
* Then use this repository in one of two ways:
  * **Give it to an AI coding agent** (what I did, and what I'd recommend). Claude Code, Codex
    or similar can set it up, babysit the long runs, read the logs and show you the results.
    [docs/AGENT.md](docs/AGENT.md) has a prompt you can paste.
  * **Run it yourself, no AI needed.** [docs/RUN-IT-YOURSELF.md](docs/RUN-IT-YOURSELF.md) walks
    through every step.

It needs patience more than skill. The full check of every video frame runs at a few times real
time on a laptop, so a full card takes a day or more. Every stage can be stopped and resumed.

---

## What it recovers

**Videos:** full-resolution `.insv` VIDs and their `.lrv` previews, byte-for-byte as they were on
the card: `VID_<date>_<time>_00_<nnn>.insv`, `LRV_..._01_<nnn>.lrv`. Each one gets a
`MANIFEST.csv` row with its size, duration, streams, decoded/expected frame counts, decode exit
codes and error lines, how the picture was checked, its SHA-256, and the cluster chain it came
from. The Insta360 trailer (gyro/IMU, lens calibration, thumbnails) is rebuilt when all its
records survived.

A video is published only when **all** of these hold:

1. its size and every stream match the camera's own sample tables;
2. a full FFmpeg decode of every stream exits 0 with **zero** error lines and decodes exactly the
   expected number of frames;
3. its picture matches the companion recording (VID vs LRV, at 8 points through the clip), or a
   person approved its contact sheet;
4. sampled clusters are byte-identical to the card image.

**Photos:** 360° `.insp` photos (`IMG_<date>_<time>_00_<nnn>.insp`, 72 MP), with `PHOTOS.csv`. A
photo is published only when its JPEG data decodes bit-exactly (every restart interval ends exactly
at its marker), the two copies of the stitched preview stored inside the file are byte-identical,
the Insta360 trailer's record chain is intact, and FFmpeg decodes it without a single warning. See
[docs/PHOTOS.md](docs/PHOTOS.md).

Nothing is guessed. A cluster that cannot be proven is left unresolved and the file is not
published.

## Requirements

* Python 3.10+ (standard library only)
* In **bash**, FFmpeg/ffprobe 6.x plus gcc (to build the small photo checker). Natively on
  Linux/macOS; on Windows inside WSL: `wsl --install`, then in Ubuntu
  `sudo apt install ffmpeg build-essential`.
* A card image: an `.rdr` image (R-Drive Image), or a raw image (`dd`, `.img`) of the card. The work
  folder needs free space on a local disk, roughly the size of the recordings. The image itself
  can stay on a NAS.
* Card layout: exFAT with **256 KiB clusters** (the usual for large SDXC cards).
  `python recovery/card_info.py <image>` checks this.

Never write to the card itself. Image it first, keep the image read-only.

## Quick start

```bash
cp recovery.example.json recovery.json       # then edit: work folder + image paths
python recovery/card_info.py card1           # check the card layout
python recovery/run_recovery.py --image card1          # videos (hours to days, resumable)
python recovery/photos/run_photos.py --image card1     # photos (about half an hour per card)
```

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

The photo run already puts every verified photo in `outputs/photos/`. Open
`work/photos/review-<image>.html` and look at every photo. Then list the ones you checked in
`work/photos/visual-ok-<image>.txt` (or a single `*`), and run
`python recovery/photos/run_photos.py --image card1 --stages publish` so `PHOTOS.csv` records it.

## Stages

Videos (`run_recovery.py`):

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

Photos (`photos/run_photos.py`):

| stage | what it does |
|---|---|
| `scan` | reads every cluster no video uses, once, and records photo features |
| `walk` | rebuilds each photo's cluster chain, in the order the photos were taken |
| `check` | bit-exact decode, preview copies, trailer chain, FFmpeg |
| `retry` | re-walks failures with the verified photos' clusters off limits |
| `review` | an HTML page with both previews of every photo |
| `publish` | hard-links verified photos into `outputs/photos/<yyyy-mm>/`, writes `PHOTOS.csv` |

See [docs/METHOD.md](docs/METHOD.md) and [docs/PHOTOS.md](docs/PHOTOS.md) for how it works, and
[docs/OPERATIONS.md](docs/OPERATIONS.md) for tuning and troubleshooting.

## Limits

* Clusters that were overwritten after the format are gone. A recording that lost one is not
  published (its preview often still is). [docs/OPERATIONS.md](docs/OPERATIONS.md) shows how to
  patch a video whose start was overwritten.
* Blind clusters and lost chains can only be proven for VIDs with a recovered LRV (the LRV is the
  reference picture).
* Tested on Insta360 X5 files. Other Insta360 models use the same video container and should work.
  The photo walker knows the X5 `.insp` layout (72 MP, 2560×1280 preview), so other models' photos
  may need small changes. Other cameras need their own layout rules.

## License

MIT. See [LICENSE](LICENSE).

This is an independent project, not affiliated with or endorsed by Insta360. The name is used
only to say which cameras it works with.
