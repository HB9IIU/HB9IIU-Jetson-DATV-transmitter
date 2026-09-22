#!/bin/sh
# Mounts the USB partition named as $1 (e.g. "sda1"), forcing ownership to
# the "daniel" user app.py actually runs as - see the udev rule
# (99-usb-video-key-automount.rules) that calls this on every USB insert.
#
# Uses the kernel's plain mount(8) directly, NOT `udisksctl mount` -
# udisksctl has its own restricted allowlist of mount options (a security
# feature, since normally any logged-in user can call it over D-Bus), and
# real testing on hardware (2026-09-22) hit
# "OptionNotPermitted: Mount option `uid=1000' is not allowed" from it.
# This script only ever runs as root (always true - it's invoked via
# udev's own RUN+=), and root isn't subject to that allowlist at all, so
# going straight to mount(8) sidesteps the restriction entirely instead of
# fighting it.
#
# Can be run and tested by hand, without touching udev/USB at all:
#   sudo /usr/local/bin/usb-video-key-mount.sh sda1
# and any real error prints straight to the terminal.

set -eu
DEVICE="/dev/$1"
UID_NUM="$(id -u daniel)"
GID_NUM="$(id -g daniel)"
FSTYPE="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"
LABEL="$(blkid -o value -s LABEL "$DEVICE" 2>/dev/null || true)"
[ -n "$LABEL" ] || LABEL="$1"
MOUNTPOINT="/media/daniel/$LABEL"

mkdir -p "$MOUNTPOINT"

case "$FSTYPE" in
    vfat|exfat|ntfs)
        # These filesystem types have no (or only emulated) native Unix
        # permission bits - the uid=/gid= mount options are how ownership
        # gets set instead, and are exactly what udisksctl rejected above.
        mount -t "$FSTYPE" \
            -o "uid=$UID_NUM,gid=$GID_NUM,dmask=0022,fmask=0133" \
            "$DEVICE" "$MOUNTPOINT"
        ;;
    *)
        # ext4 and similar already have real Unix permissions on-disk -
        # mount plainly, then make sure daniel can still write into it
        # regardless of whatever uid actually owns the files on it.
        mount "$DEVICE" "$MOUNTPOINT"
        chmod 0777 "$MOUNTPOINT" || true
        ;;
esac
