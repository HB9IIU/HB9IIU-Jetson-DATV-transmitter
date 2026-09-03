#!/usr/bin/env bash
set -euo pipefail

src_dir="$(cd "$(dirname "$0")" && pwd)/videos"
dst_dir="$src_dir/normalized"
mkdir -p "$dst_dir"

for src in \
  "$src_dir/A SATURDAY AT JEBEL AFEET.mov" \
  "$src_dir/Acrylic paints.webm" \
  "$src_dir/Africa 4K - Scenic Relaxation Film With Calming Music.mp4"
do
  name="$(basename "${src%.*}")"
  final="$dst_dir/$name-datv-720p25.mp4"
  part="$final.part.mp4"
  if [[ -s "$final" ]]; then
    echo "SKIP $final"
    continue
  fi
  rm -f "$part"
  echo "START $(date -u +%FT%TZ) $src"
  gst-launch-1.0 -e \
    uridecodebin uri="file://$src" name=dec \
    dec. ! queue max-size-time=3000000000 ! nvvidconv ! video/x-raw,format=I420 ! \
      videoscale add-borders=true ! videorate ! \
      video/x-raw,width=1280,height=720,framerate=25/1,pixel-aspect-ratio=1/1 ! \
      nvvidconv ! video/x-raw\(memory:NVMM\),format=NV12 ! \
      nvv4l2h264enc bitrate=4000000 insert-sps-pps=true iframeinterval=25 ! \
      h264parse ! queue ! mux. \
    dec. ! queue max-size-time=3000000000 ! audioconvert ! audioresample ! \
      audio/x-raw,format=S16LE,rate=48000,channels=2 ! \
      voaacenc bitrate=128000 ! aacparse ! queue ! mux. \
    mp4mux name=mux faststart=true ! filesink location="$part"
  mv "$part" "$final"
  echo "DONE  $(date -u +%FT%TZ) $final"
done
