#!/bin/sh
set -e

pungi-koji --no-label --target-dir /mnt/koji/compose/mizdebsk --config pungi.conf

# Layer our buildroot on top of regular buildroot
rm -rf merged_repo
mergerepo_c \
    --repo file:///mnt/koji/compose/mizdebsk/latest-Fedora-Java/compose/Buildroot/x86_64/os/ \
    --repo file:///mnt/fedora_koji_prod/koji/repos/f29-build/latest/x86_64/
rm -rf /mnt/koji/compose/mizdebsk/hybrid-buildroot
mv merged_repo /mnt/koji/compose/mizdebsk/hybrid-buildroot
