# How the photo recovery works

The same problem as for the videos: after a quick format nothing on the card says which clusters
belonged to which photo, or in what order. Photos are easier in one way (one file at a time, no
interleaved preview file) and harder in another (no `moov` table saying where everything is).
What makes them recoverable is that an Insta360 X5 `.insp` file is extremely redundant, so every
cluster can be proven.

Unit of work, as for videos: one chunk of the image = one exFAT cluster = 256 KiB.

## Anatomy of an X5 `.insp` (72 MP photo, about 18–28 MB)

| part | size | what it is |
|---|---|---|
| SOI + APP1 Exif | ~9–12 KB | camera metadata, an embedded 320×160 thumbnail, and the camera path `DCIM\Camera01\IMG_<date>_<time>_00_<nnn>.ins` |
| 75 APP2 segments of 65,535 bytes + 1 of 227 | 4,915,504 bytes (4,915,200 of payload) | a 2560×1280 stitched preview (8-bit YUV 4:2:0, NV12) |
| DQT, SOF0, DHT, DRI, SOS | 593 bytes | baseline JPEG 11904×5952, 4:2:0, restart interval 65,535 MCUs |
| scan data | ~8–18 MB | the photo itself: two fisheye circles side by side |
| EOI | 2 bytes | |
| Insta360 trailer | ~5 MB | small records (info, calibration, …), then record 0x2 = 40-byte header + **the same 4,915,200-byte preview again**, then a 78-byte tail (record header, 32 zero bytes, trailer length, version 3, magic `8db42d694ccc418790edff439fe026bf`) |

Three things follow from this:

* **Every photo names itself.** The camera path in the Exif gives the original file name
  (cut off at `.ins`; the tool restores `.insp`).
* **The header region is fully predictable** from the Exif length: every APP2 marker
  (`FF E2 FF FF`) sits at a known offset, and so do the five JPEG segments after the last one.
* **The preview is stored twice.** Once the trailer's record 0x2 is found, every byte from
  there to the end of the file is known. The APP2 copy and the trailer copy also prove each other.

## 1. Scan (`scan_free.py`)

Every cluster that no recovered video uses is read once. Clusters that are all one byte value, or
whose RDR record is tiny, are only listed. For each of the others, the scan records:

* whether it starts a JPEG, and the camera path, Exif length and model;
* the offsets of `FF E2 FF FF` (APP2 headers);
* whether it is JPEG scan data: every `FF` byte followed by `00`, `D0`–`D7`, `D9` or `FF`. Also
  the count of `FF 00` pairs: scan data has thousands, the preview tables none, video a handful.
* where scan data stops (with the offset of `FF D9`, EOI) or starts inside it;
* the offsets of the trailer magic and of the record-0x2 header;
* a SHA-1 of the whole cluster.

Photo-like clusters are cached locally, so the later stages hardly touch the image.

## 2. Walk (`walk_photo.py`)

The camera allocates clusters **first-fit**: a new file fills the lowest free clusters first,
which are often single holes left between earlier files, and then continues in the main free run.
So a photo's clusters are free clusters in ascending order. Photos are walked **in the order they
were taken**: an earlier photo owns its clusters, and a later photo starting in a hole before it
would otherwise walk straight into its data.

* **Header region.** A candidate for cluster *k* must carry `FF E2 FF FF` at exactly the predicted
  offsets, and the DQT…SOS markers where they are predicted.
* **Scan region.** A candidate must be JPEG scan data. Every few clusters, and at EOI, the prefix
  built so far is checked with `jpegmcu` (below). If the check fails, the walk backtracks: for
  each of the last ~12 clusters it tries the next candidates and re-extends, until the prefix
  decodes again.
* **Trailer.** The cluster holding the record-0x2 header is found right after EOI. From there,
  every remaining cluster's exact content is known, so it is looked up by SHA-1 anywhere on the
  card. The last cluster must hold the magic at the predicted offset and match the predicted
  bytes. It is often "tiny" (a few KB of file, then zeros), and is then read straight from the
  image.

A header or trailer cluster that cannot be found anywhere can be rebuilt byte-exactly from the
other preview copy. The tool marks it `rebuilt-exact`. A missing scan cluster cannot be rebuilt;
that photo stays unverified.

## 3. Checks (`check_photo.py`, `jpegmcu.c`)

`jpegmcu` is a small baseline JPEG entropy decoder. It decodes every Huffman symbol of every block
(no IDCT) and counts MCUs. It requires each restart interval to end exactly at its `RSTn` marker
(numbered 0…7 in order) with only 1-bit padding, the last interval to end exactly at EOI, and no
invalid code or block of more than 64 coefficients. A single wrong cluster anywhere in the scan
data breaks this. In tests it caught 100% of spliced clusters: zeros, clusters of another
photo's scan data, an own cluster from a different position, a dropped or duplicated cluster. It
always caught them by the next restart marker at the latest.

A photo is verified when:

1. the header parses (SOI, Exif, APP2 run, SOF0 baseline, DRI, SOS);
2. the scan data is entropy-valid up to EOI;
3. the trailer length field equals the bytes after EOI, the magic is right, and the backward chain
   of record headers lands exactly on the first byte after EOI;
4. the two preview copies (APP2 and record 0x2) are byte-identical;
5. `jpegmcu` passes (276,768 MCUs for a 72 MP photo);
6. FFmpeg decodes it with error detection on and prints nothing.

The review page then shows each photo twice: as stored (two fisheye circles) and as the stitched
preview, for the human check.

## Results on the two cards this was built for

41 photo starts were found and all 41 were verified: 19 on one card, 22 on the other. Most walked
straight through. Two from a burst on the second card initially failed. One of them had its first
19 clusters in single holes left between the earlier photos of the burst, and walking it before
those earlier photos made it claim their data. Walking photos in the order they were taken fixed
both. As an independent check, every photo that an earlier generic carving tool had recovered
intact was byte-identical to the rebuilt one.
