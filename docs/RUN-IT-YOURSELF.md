# Run it yourself (no AI needed)

This guide takes you from "I just formatted my card" to verified files you can open in Insta360
Studio. You type a handful of commands. The computer does the rest, but it takes a while: plan on a
day or more per card for the videos, mostly unattended.

If you get stuck, [AGENT.md](AGENT.md) has a prompt that hands the whole thing to an AI coding
agent.

## 0. Before anything else

* **Stop using the card.** Every recording or photo you take now can overwrite what you want back.
* Don't format it again, and don't run "repair" or "fix disk" tools on it. Recovery software that
  only *reads* is fine.
* You need a computer with free disk space of at least **twice the card size**: one copy for the
  card image, plus room for the recovered files. The image can also sit on a NAS or an external
  drive.

## 1. Image the card

An image is a sector-by-sector copy of the whole card. All the work happens on the image, so the
card stays untouched as your backup.

**Windows.** R-Drive Image (a paid tool with a trial) is what I used. Choose *Create an Image*,
pick the card's *disk* (not just the partition), and save it as a `.rdr` file with compression on.
Any other tool that makes a raw sector-by-sector image (`.img`) works too.

**macOS.** Insert the card, then find its disk number:

```bash
diskutil list                       # look for the card, e.g. /dev/disk4 (check the size!)
diskutil unmountDisk /dev/disk4
sudo dd if=/dev/rdisk4 of=~/card1.img bs=4m status=progress
```

**Linux.**

```bash
lsblk                               # find the card, e.g. /dev/sdb (check the size!)
sudo apt install gddrescue
sudo ddrescue /dev/sdb ~/card1.img ~/card1.map
```

`dd`/`ddrescue` copy *from* `if=` / the first device *to* the image. Double-check the device name:
swapping them, or picking the wrong disk, destroys data.

When the image is done, eject the card and put it away.

## 2. Install what it needs

You need Python 3.10 or newer, plus FFmpeg and gcc in a bash shell.

**Windows.** Install Python from python.org, then WSL (this gives you Ubuntu inside Windows). In
PowerShell:

```powershell
wsl --install
```

Restart if asked, open "Ubuntu" from the Start menu, create a user, then:

```bash
sudo apt update && sudo apt install -y ffmpeg build-essential
```

**macOS.**

```bash
xcode-select --install          # compilers (gcc)
brew install python ffmpeg      # needs Homebrew: https://brew.sh
```

**Linux (Debian/Ubuntu).**

```bash
sudo apt install -y python3 ffmpeg build-essential git
```

## 3. Get the tool and point it at your image

```bash
git clone https://github.com/frannraf/insta360-card-recovery.git
cd insta360-card-recovery
cp recovery.example.json recovery.json
```

(No git? Use *Code → Download ZIP* on GitHub and unzip it.)

Edit `recovery.json` in any text editor:

```json
{
  "work": "D:/insta360-recovery/work",
  "outputs": "D:/insta360-recovery/outputs/recovered",
  "images": {
    "card1": "D:/card-images/card1.rdr"
  }
}
```

* `work` is where all the intermediate data goes. It **must be on a local disk** with room
  roughly the size of your recordings.
* `outputs` is where the verified videos end up. Photos go next to it, in `outputs/photos`.
* `images` gives each card image a short name. With two cards, add `"card2": "..."`.

Use forward slashes in paths, even on Windows.

## 4. Check the card layout

```bash
python recovery/card_info.py card1
```

You want `"verdict": "ok"`. The tool needs exFAT with 256 KiB clusters; my X5 cards had them.
One catch: this reads the card's *current* layout. If the card was reformatted on a PC with a
different allocation size, the layout it reports is not the one your files were written with.
The video tools then refuse to run, and the photo tool warns and scans the whole image anyway.

## 5. Recover the videos

```bash
python recovery/run_recovery.py --image card1
```

Leave it running. It goes through all the stages in the README and saves its progress, so if you
stop it, or the computer restarts, run the same command again and it carries on. To see how far it
has got, open a second terminal and run:

```bash
python recovery/recovery_status.py
```

When it finishes, a few recordings may need your eyes. The automatic check compares each video
with its low-res preview, and some scenes (darkness, fast motion) are hard to compare
automatically. Those are listed in `work/review/needs-eyes-card1.txt`:

```bash
python recovery/run_recovery.py --image card1 --stages review
```

Open the contact sheets in `work/review/` (`card1-sheets-*.jpg`). Each row compares the full video
with its preview at several points. If a row looks right (same scene, no smearing, no grey or
green blocks), add its label to `work/review/approved-card1.txt`, one per line. Then:

```bash
python recovery/run_recovery.py --image card1 --stages publish,inst,audit
```

## 6. Recover the photos

```bash
python recovery/photos/run_photos.py --image card1
```

This reads the parts of the card that the videos don't use, rebuilds every 360° photo it finds,
checks each one, and puts the verified photos in `outputs/photos/`. It takes roughly 15–40
minutes per card, depending on how fast the image can be read. Then open
`work/photos/review-card1.html` in a browser. Every photo is shown twice: as stored (two fisheye
circles) and as the stitched preview the camera keeps inside the file. Look through them. When
you're happy, create `work/photos/visual-ok-card1.txt` containing a single `*` (or the names of
the photos you checked), and run this so `PHOTOS.csv` records that you looked:

```bash
python recovery/photos/run_photos.py --image card1 --stages publish
```

You can run the photo step before or without the video step. It then scans the whole card, which
just takes longer.

## 7. Your files

* `outputs/recovered/` holds the videos: `VID_..._00_nnn.insv` (full resolution) and
  `LRV_..._01_nnn.lrv` (the preview Insta360 Studio uses for scrubbing). Keep the names as they
  are: Studio pairs VID and LRV by name. `MANIFEST.csv` lists every file with its checks and
  SHA-256.
* `outputs/photos/<yyyy-mm>/` holds the photos, `IMG_..._00_nnn.insp`, listed in
  `outputs/photos/PHOTOS.csv`.

Open them in Insta360 Studio (File → Open). Then **copy them to two places** (an external drive and
a NAS or cloud) before you think about deleting the card images.

## When something doesn't come back

* `python recovery/recovery_status.py` lists every recording that isn't published and why.
* A video whose beginning was overwritten can sometimes be **patched**: everything after the first
  keyframe is perfect, and the first second or two is damaged. See "Patched files" in
  [OPERATIONS.md](OPERATIONS.md).
* A photo that the walker can't complete is listed under `not_verified` at the end of the photo
  run. Its chain file in `work/photos/chains/` says where it stopped.
* Keep the card images until you're sure you have everything. They are the only way to try again.

## How long it takes (laptop, 8 cores)

| step | 256 GB card |
|---|---|
| imaging the card | 1–3 hours |
| videos: finding and chaining every file | a few hours |
| videos: decoding every frame to check it | a day or more |
| photos | 15–40 minutes |
