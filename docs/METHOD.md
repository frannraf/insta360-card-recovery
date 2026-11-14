# How the recovery works

## The problem

Deleting files (or a quick format) on exFAT clears the directory entries and
the allocation bitmap; with no FAT chain for contiguous files, nothing on the
card says which clusters belonged to which file, or in what order. A 360°
camera makes this hard: every recording is two files written at once, they
interleave, and the start of each file is scattered into holes.

Unit of work: one **chunk** of the image = one exFAT **cluster** = 256 KiB.

## What we know about the card (Insta360 X5)

* The camera writes the VID (two HEVC streams + AAC) and the LRV (H.264
  preview, both fisheyes side by side) concurrently: long VID runs interleaved
  with short (8-cluster) LRV runs.
* A new file's header (and sometimes its first clusters) goes **first-fit**
  into single free clusters earlier on the card, often spaced ~3,500–3,700
  clusters apart; then the data continues in a main run next to its LRV.
* The `moov` atom follows `mdat` in the same cluster, so the cluster holding
  the start of the `moov` is the file's last data cluster ("last-pinned").
* The Insta360 trailer (`inst`) is not contiguous: cluster-aligned records
  listed in an index in the file's final cluster; time-series records (IMU,
  exposure, …) are written as VID/LRV duplicate cluster pairs during the
  recording, the thumbnail strip and final records right after the `moov`.

## 1. Catalog: what files existed

A marker scan finds every MP4 header (`ftyp`… `mdat`) and every `moov` atom on
the card. Headers and `moov`s are paired by the exact `mdat` end, giving each
recording's size, duration and complete sample tables (every frame's byte
offset and size inside the file).

## 2. Chains: walking the bitstream

Video samples are sequences of length-prefixed NAL units. Knowing every
sample's offset and size, the walker predicts, for the next 256 KiB of the file,
exactly where NAL units must begin, and tests candidate chunks against those
predictions (valid length that fits the sample, valid NAL header).

Evidence is counted conservatively:

* only independent anchors count: sample starts from the tables and boundaries
  carried over from the previous cluster (≤ 15 bits each), plus exact landings
  on our own sample ends (+15 bits); NAL chains inside some other video stream
  are self-consistent and earn nothing;
* a contiguous step needs ≥ 10 bits, a jump needs ≥ 40 bits accumulated over
  the target and up to 12 contiguous followers.

## 3. Blind clusters: decode against the preview

A cluster wholly inside one large NAL (a big I-frame slice is often 1–2 MB)
has no boundary to check. It is placed only by decoding:

1. Candidates are the chunks between its two proven neighbours. A NAL payload
   never contains the byte sequences `00 00 00`, `00 00 01`, `00 00 02` (emulation
   prevention), which rejects about half of them without decoding.
2. Each remaining candidate is substituted, the GOP up to the affected frame
   is decoded (dozens of candidates per FFmpeg run, each unit starting at an
   IDR so they cannot influence each other), and the frame is compared with
   the companion LRV frame in 26 row bands.
3. The top ten are decoded again one at a time (inside a batch, a garbage
   unit can inherit picture from the previous unit's frame buffer).
4. A placement is applied only if the winner matches the LRV in **all 26
   bands** (≥ 28 dB) and the best candidate with different bytes matches at
   most 16. Correct chunks score 26/26 at 33–38 dB; wrong ones 1–9 bands at
   12–22 dB.

When a file has two blind clusters in the same frame, each one is scored with
the other in place, repeating until the answers stop changing.

## 4. Lost chains: one decode-confirmed step at a time

Sometimes the walk stops right after the header: cluster 1 sits in a far hole
and carries a single NAL boundary (~15 bits), not enough to accept it. The
`lost` stage then loops:

* walk strictly from the last confirmed cluster;
* at the break, probe forward for chunks that satisfy the predicted boundary;
* decode the frame with each candidate appended and count 16×16 blocks that
  match the LRV; accept the candidate that decodes with zero errors and adds
  clearly more matching picture than any other clean candidate (if a blind
  cluster precedes it, the pair is searched together);
* repeat until the walk completes.

## 5. Validation: every frame, every stream

Each assembled file gets a full FFmpeg decode of every stream with strict
error detection (`-err_detect +crccheck+bitstream+buffer`). Pass = exit 0,
zero error lines, and decoded frame count equal to the sample count in the
`moov` for every video stream. Software decoding is deliberate: hardware
decoders conceal errors instead of reporting them.

## 6. Picture check: VID vs LRV

The VID and the LRV are recovered from different clusters, so if both show
the same picture, both are right. At 8 points through the clip each VID lens
frame is compared with both LRV halves (luma PSNR at 416×416). Approval needs
a median of at least 28 dB, at most two points below 24 dB (in fast camera
spins the two copies can be a frame apart) and no point below 18 dB. Verified
files score 28–38 dB at most points; wrong pictures 8–18 dB. Night scenes and
covered lenses match too: both copies are equally dark. Anything that does not
pass goes to a person.

## 7. Publishing and trailers

Published files are hard links to the assembled files (no extra space) with
SHA-256 in the manifest. The trailer is rebuilt record by record from the
duplicate clusters (headers verified in place, IMU timestamps monotonic
across records) and only the trailer bytes are patched, with the hash of
everything before it proven unchanged. Finally sampled clusters of every
published file are compared byte-for-byte with the image.

## Safety

* The image is only ever opened read-only.
* Nothing is published on partial evidence; every rule above refuses rather
  than guesses.
* Chain edits are backed up (`*.pre-trial.json`, `*.pre-resume*.json`).
