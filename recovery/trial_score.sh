#!/usr/bin/env bash
# trial_score.sh ref <lrv> <g0> <g1> <outL> <outR>
# trial_score.sh cand <es> <fmt> <refL> <refR>
#   prints: <error_lines> <frames> <psnrL> <psnrR>
set -u
mode="$1"; shift
if [ "$mode" = ref ]; then
  lrv="$1"; g0="$2"; g1="$3"; oL="$4"; oR="$5"
  ffmpeg -nostdin -v error -i "$lrv" -vf "select=between(n\,$g0\,$g1),crop=832:832:0:0,scale=416:416,format=gray" -fps_mode passthrough -f rawvideo -y "$oL"
  ffmpeg -nostdin -v error -i "$lrv" -vf "select=between(n\,$g0\,$g1),crop=832:832:832:0,scale=416:416,format=gray" -fps_mode passthrough -f rawvideo -y "$oR"
  exit 0
fi
es="$1"; fmt="$2"; rL="$3"; rR="$4"; step="${5:-1}"
g="${es}.gray"
ffmpeg -nostdin -hide_banner -v error -threads 4 -f "$fmt" -i "$es" -vf "select=not(mod(n\,$step)),scale=416:416,format=gray" -fps_mode passthrough -f rawvideo -y "$g" 2> "${es}.log"
el=$(wc -l < "${es}.log")
fr=$(( $(stat -c %s "$g") / 173056 ))
pL=$(ffmpeg -nostdin -v info -f rawvideo -pix_fmt gray -s 416x416 -i "$g" -f rawvideo -pix_fmt gray -s 416x416 -i "$rL" -lavfi psnr -f null - 2>&1 | grep -o 'average:[0-9.inf]*' | head -1 | cut -d: -f2)
pR=$(ffmpeg -nostdin -v info -f rawvideo -pix_fmt gray -s 416x416 -i "$g" -f rawvideo -pix_fmt gray -s 416x416 -i "$rR" -lavfi psnr -f null - 2>&1 | grep -o 'average:[0-9.inf]*' | head -1 | cut -d: -f2)
rm -f "$g"
echo "$el $fr ${pL:-0} ${pR:-0}"
