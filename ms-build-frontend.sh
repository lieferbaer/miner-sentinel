#!/bin/bash
# build frontend while clearing cPanel virtfs mirrors of podman overlay mounts
cd /opt/minersentinel
clean() { grep '/home/virtfs/' /proc/self/mountinfo | awk '{print $5}' | grep '/var/lib/containers/storage/overlay' | sort -r | xargs -r -n1 umount -l 2>/dev/null; }
clean
( while sleep 1; do clean; done ) & LOOP=$!
ok=0
for try in 1 2 3; do
  if podman build -t localhost/minersentinel_frontend:latest --target production frontend > /root/fe-build7.log 2>&1; then ok=1; break; fi
  echo "build try $try failed" >> /root/fe-build7.status; sleep 3
done
kill $LOOP
if [ $ok = 1 ]; then ./ms-recreate.sh frontend minersentinel-web > /root/fe-recreate7.log 2>&1; echo BUILT >> /root/fe-build7.status; else echo FAILED >> /root/fe-build7.status; fi
echo FIN >> /root/fe-build7.status
