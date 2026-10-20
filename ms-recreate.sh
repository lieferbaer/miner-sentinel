#!/bin/bash
# Recreate one MinerSentinel container on this cPanel host.
# cPanel's jailshell (virtfs) mirrors container mounts under /home/virtfs/*,
# which makes "podman rm" fail with "device or resource busy". Unmount those
# mirrors first. Usage: ms-recreate.sh <compose-service> <container-name>
set -u
svc=$1; name=$2
id=$(podman inspect -f '{{.Id}}' "$name" 2>/dev/null)
layer=$(podman inspect -f '{{.GraphDriver.Data.MergedDir}}' "$name" 2>/dev/null | awk -F/ '{print $(NF-1)}')
for key in $id $layer; do
  [ -n "$key" ] || continue
  grep "/home/virtfs/" /proc/self/mountinfo | grep "$key" | awk '{print $5}' | sort -r | xargs -r -n1 umount -l
done
podman rm -f "$name" || { sleep 2; [ -n "$id" ] && podman rm -f --storage "$id"; }
cd /opt/minersentinel && podman-compose up -d --no-deps "$svc"
