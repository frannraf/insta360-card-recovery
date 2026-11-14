#!/usr/bin/env bash
# drop_cache_loop.sh [pidfile]: every 15 s flush the Linux page cache (page
# cache, dentries, inodes, buffers; no settings are changed).  Multi-GB decodes
# over /mnt/<drive> fill the WSL VM's cache until Windows runs short of RAM and
# reads fail with ENOMEM.  Must run as root.  Writes its PID to [pidfile] so
# the caller can stop exactly this loop.
[ -n "${1:-}" ] && echo $$ > "$1"
while true; do
  sync
  echo 3 > /proc/sys/vm/drop_caches
  sleep 15
done
