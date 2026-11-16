# Hand it to an AI coding agent

This is how I did it, and what I'd recommend if you don't want to babysit a multi-day run. A coding
agent that can run commands on your computer (Claude Code, Codex, or similar) can set this up, keep
the long runs going, read the logs, and show you what came back. You stay in charge of the
decisions: what to delete, and whether a picture looks right.

## Before you start

1. **Stop using the card and image it first** ([how](RUN-IT-YOURSELF.md#1-image-the-card)). Do this
   step yourself. An agent should never touch the card itself.
2. Download or clone this repository somewhere with free space.
3. Start your agent in that folder and paste the prompt below, filling in the parts in `<...>`.

## The prompt

```text
I accidentally quick-formatted the SD card from my Insta360 camera and want my videos and
photos back. I made an image of the card: <path to the .rdr or .img file>.
(<If you have more than one card, list each image.>)

This repository recovers Insta360 recordings and photos from a card image. Before doing
anything, read README.md, docs/METHOD.md, docs/PHOTOS.md, docs/OPERATIONS.md and
docs/RUN-IT-YOURSELF.md.

Rules:
- Treat the card images as read-only and irreplaceable. Never write to them, move them or
  delete them. Never touch the physical card.
- Put all intermediate data under the configured work folder, on a local disk with enough
  free space (<which drive or folder to use>). Tell me if space runs low; don't delete
  anything to make room without asking me.
- Only files that pass every check in the README count as recovered. Never call a file
  recovered because it opens, or because ffprobe recognizes it.
- Ask me before installing anything that needs administrator rights, and before deleting
  anything.

Steps:
1. Install or check the requirements (Python 3.10+, FFmpeg and gcc in bash; on Windows,
   through WSL).
2. Create recovery.json from recovery.example.json, and run card_info.py for each image.
3. Run the video recovery (run_recovery.py) for each image. It takes a long time. Keep it
   running, resume it if it stops, and give me short progress updates
   (recovery_status.py).
4. When it needs a human picture check, show me the contact sheets and wait for my OK
   before approving anything.
5. Run the photo recovery (recovery/photos/run_photos.py) for each image. Show me the review
   page, and record the photos I've looked at.
6. Report what was recovered and what wasn't, with the manifests (MANIFEST.csv,
   PHOTOS.csv), and explain anything that failed.
```

## What to expect

* The agent will mostly be waiting. The full video check decodes every frame of every recording,
  which takes a day or more per card on a laptop. Long gaps between updates are normal.
* It may hit things these tools haven't seen: a card from a different model, a different cluster
  size, a recording whose start was overwritten. Ask it to explain what it found before it changes
  any code. [OPERATIONS.md](OPERATIONS.md) has the known cases, including how to *patch* a video
  whose beginning is gone.
* Ask for a plain-language summary at the end: how many videos and photos came back, and which
  ones didn't and why.
* Afterwards, copy the results to two places before you even think about deleting the card images.
