#!/usr/bin/env bash
# fast_decode.sh <es> <out.gray> [fmt]: decode an Annex-B batch to 416x416 gray
# frames; prints "<ffmpeg exit> <error lines>".  Run from the work folder
# (paths are relative to it).
set -u
es="$1"; out="$2"; fmt="${3:-hevc}"
ffmpeg -nostdin -hide_banner -v error -threads 8 -f "$fmt" -i "$es" \
  -vf "scale=416:416,format=gray" -fps_mode passthrough -f rawvideo -y "$out" 2> "$out.log"
echo "$? $(wc -l < "$out.log")"
