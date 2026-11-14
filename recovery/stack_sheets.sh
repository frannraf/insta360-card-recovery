#!/usr/bin/env bash
# Usage: stack_sheets.sh <out.jpg> <sheet1.jpg> [sheet2.jpg ...]  (all sheets same width)
set -u
out="$1"; shift
n=$#
args=(); for s in "$@"; do args+=(-i "$s"); done
if [ "$n" -eq 1 ]; then cp "$1" "$out"; exit 0; fi
ffmpeg -nostdin -hide_banner -v error "${args[@]}" -filter_complex "vstack=inputs=$n" -frames:v 1 -q:v 4 -y "$out"
echo "$out"
