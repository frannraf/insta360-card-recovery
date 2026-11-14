#!/usr/bin/env bash
# Build a contact sheet (rows = video streams, cols = 2%/50%/97%) from validation PNGs.
# Usage: make_sheet.sh <validation_dir> <label>
set -u
d="$1"; label="$2"
shopt -s nullglob
files=("$d"/v*_at_*.png)
n=${#files[@]}
[ "$n" -eq 0 ] && { echo "no frames"; exit 1; }
inputs=(); for f in "$d"/v0_at_0.02.png "$d"/v0_at_0.5.png "$d"/v0_at_0.97.png "$d"/v1_at_0.02.png "$d"/v1_at_0.5.png "$d"/v1_at_0.97.png; do
  [ -f "$f" ] && inputs+=(-i "$f")
done
cnt=$(( ${#inputs[@]} / 2 ))
if [ "$cnt" -eq 6 ]; then
  layout="0_0|w0_0|w0+w1_0|0_h0|w0_h0|w0+w1_h0"
else
  layout="0_0|w0_0|w0+w1_0"
fi
ffmpeg -nostdin -hide_banner -v error "${inputs[@]}" \
  -filter_complex "xstack=inputs=$cnt:layout=$layout,scale=1200:-2,drawtext=text='$label':x=8:y=8:fontsize=22:fontcolor=yellow:box=1:boxcolor=black@0.6" \
  -frames:v 1 -y "$d/sheet.jpg" 2>/dev/null || \
ffmpeg -nostdin -hide_banner -v error "${inputs[@]}" \
  -filter_complex "xstack=inputs=$cnt:layout=$layout,scale=1200:-2" -frames:v 1 -y "$d/sheet.jpg"
echo "$d/sheet.jpg"
