#!/usr/bin/env bash
# Full validation, one decode pass per stream (streams decoded in parallel).
# Usage: validate_media2.sh <file> <outdir>
# Produces: probe.json, decode-<s>.log (error-level output only),
# <s>.crc (one line per decoded frame -> exact decoded frame count),
# result.txt, and PNG frames at 2%/50%/97% of every video stream.
set -u
f="$1"; out="$2"
mkdir -p "$out"
ffprobe -v error -show_entries format=duration,size:stream=index,codec_name,codec_type,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration,sample_rate,channels \
  -of json "$f" > "$out/probe.json"
pe=$?
nv=$(ffprobe -v error -select_streams v -show_entries stream=index -of csv=p=0 "$f" | wc -l)
na=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$f" | wc -l)
decode() {  # decode <stream-name>: v<i> or a<i>, frame CRCs to <s>.crc
  local s=$1 t=${1:0:1} i=${1:1}
  if [ "$t" = v ]; then
    ffmpeg -nostdin -hide_banner -v error -threads 4 -thread_type frame -err_detect +crccheck+bitstream+buffer -i "$f" -map 0:v:$i -f framecrc -y "$out/$s.crc" 2> "$out/decode-$s.log"
  else
    ffmpeg -nostdin -hide_banner -v error -err_detect +crccheck+bitstream+buffer -i "$f" -map 0:a:$i -f framecrc -y "$out/$s.crc" 2> "$out/decode-$s.log"
  fi
}
pids=(); names=()
for ((i=0;i<nv;i++)); do decode "v$i" & pids+=($!); names+=("v$i"); done
for ((i=0;i<na;i++)); do decode "a$i" & pids+=($!); names+=("a$i"); done
ecs=()
for idx in "${!pids[@]}"; do wait "${pids[$idx]}"; ecs[$idx]=$?; done
# A decode that died from memory pressure (parallel 2x HEVC decodes in a small
# WSL VM; seen as "Cannot allocate memory" on read) says nothing about the
# file: rerun that stream alone, up to twice.  The retry is recorded.
retries=()
for idx in "${!names[@]}"; do
  retries[$idx]=0
  while grep -q 'Cannot allocate memory' "$out/decode-${names[$idx]}.log" && [ "${retries[$idx]}" -lt 2 ]; do
    mv -f "$out/decode-${names[$idx]}.log" "$out/decode-${names[$idx]}.oom$((retries[$idx]+1)).log"
    retries[$idx]=$((retries[$idx]+1))
    decode "${names[$idx]}"; ecs[$idx]=$?
  done
done
{
echo "file=$f"
echo "probe_exit=$pe"
for idx in "${!names[@]}"; do
  s=${names[$idx]}
  frames=$(grep -vc '^#' "$out/$s.crc")
  errs=$(wc -l < "$out/decode-$s.log")
  extra=""; [ "${retries[$idx]}" -gt 0 ] && extra=" oom_retries=${retries[$idx]}"
  echo "$s decode_exit=${ecs[$idx]} decode_error_lines=$errs decoded_frames=$frames$extra"
done
} > "$out/result.tmp" && mv -f "$out/result.tmp" "$out/result.txt"
dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
for ((i=0;i<nv;i++)); do
  for pt in 0.02 0.5 0.97; do
    t=$(awk -v d="$dur" -v p="$pt" 'BEGIN{printf "%.3f", d*p}')
    ffmpeg -nostdin -hide_banner -v error -ss "$t" -i "$f" -map 0:v:$i -frames:v 1 -vf "scale=640:-2" -y "$out/v${i}_at_${pt}.png"
  done
done
cat "$out/result.txt"
