"""USB "video key" support - lets a user add prepared videos without
growing the SD card, using the Jetson's one remaining free USB port.

Design (v2, simplified 2026-09-14 - the original background-thread +
auto-format design turned into a real mess on actual hardware: pyudev
pulled in a `six` version conflict, then the mkfs/wipefs/mount/chown sudo
dance needed a hand-written sudoers file, then a stale GPT signature from
the stick's factory partition table made a second format fail anyway.
None of that is worth it for what this feature actually needs). This
version does none of that:

  - No formatting, no sudo, no background thread. Whatever filesystem is
    already on the stick (FAT32/exFAT/ext4/whatever it shipped with) is
    used as-is - the app only ever adds the two preprocessed_<W>x<H>/
    folders to it if they're missing, and never erases anything.
  - Mounting is left entirely to the OS's own automount (udisks2, active
    on any standard Ubuntu desktop image, which JetPack ships) - this
    module only ever reads `lsblk`/`udevadm` to see where things already
    ended up. It never calls mount/umount/mkfs itself, so there is
    nothing here that needs root.
  - The web UI's Setup page lists whatever USB disk(s) are currently
    plugged in (list_candidates()) and lets the user explicitly pick one
    (select(serial)) - no automatic popup prompt on insert.
  - Once picked, that stick's serial number is remembered in a small JSON
    registry on the SD card, so future insertions of the same stick are
    picked up automatically without revisiting the Setup page -
    mounted_root() checks fresh via lsblk on every call, so there's no
    cached state that can ever go stale or need a restart to pick up a
    change.

If a plugged-in stick never gets an automount from the OS (no desktop
session, or automount disabled), list_candidates() will show it with
mounted_at=None and the Setup page says so - not something this module
tries to work around directly, since it deliberately never mounts
anything itself (see above). Confirmed on real hardware (2026-09-22): a
headless Jetson (SSH/PyCharm only, no GUI login) never automounts on its
own, since udisks2's automount trigger normally requires an active local
desktop session. The real, one-time-per-Jetson fix is
system/99-usb-video-key-automount.rules - a udev rule that runs
`udisksctl mount` automatically on insert, working headless because it
runs as root (see that file's own comments for the install command and
why). Without it installed, the one-off manual fallback is `udisksctl
mount -b /dev/sdb1` (as the same user app.py runs as), which is what
surfaced this whole gap in the first place.
"""

import json
import os
import re
import shutil
import subprocess
import sys

REGISTRY_FILENAME = "usb_video_key_registry.json"
# Kept in sync by hand with preprocess_videos.py's RESOLUTIONS - update
# both if a profile resolution is ever added/removed.
PREPROCESSED_FOLDER_NAMES = ("preprocessed_640x360", "preprocessed_960x540")
# Same name preprocess_videos.py's own SOURCE_DIR already uses for raw,
# not-yet-converted source videos - created now so it's there in advance
# for whenever raw-video-onto-the-key preprocessing gets wired up.
ORIGINAL_VIDEOS_FOLDER_NAME = "original videos"
FOLDER_NAMES_TO_CREATE = PREPROCESSED_FOLDER_NAMES + (ORIGINAL_VIDEOS_FOLDER_NAME,)

_project_dir = None
_supported = False


def init(project_dir):
    """Record where the SD-card registry file lives and check whether
    this platform can support USB-key detection at all - a no-op
    (list_candidates() stays empty forever) without lsblk/udevadm on
    PATH, or off Linux, both true of the Windows dev machine this is also
    edited on."""
    global _project_dir, _supported
    _project_dir = project_dir
    _supported = (
        sys.platform.startswith("linux")
        and shutil.which("lsblk") is not None
        and shutil.which("udevadm") is not None
    )


def _registry_path():
    return os.path.join(_project_dir, REGISTRY_FILENAME)


def _load_registry():
    try:
        with open(_registry_path()) as registry_file:
            return json.load(registry_file)
    except (OSError, ValueError):
        return {}


def _save_registry(registry):
    # Write-to-temp-then-replace so a crash mid-write never leaves a
    # truncated/corrupt registry behind.
    tmp_path = _registry_path() + ".tmp"
    with open(tmp_path, "w") as registry_file:
        json.dump(registry, registry_file, indent=2, sort_keys=True)
    os.replace(tmp_path, _registry_path())


def _boot_disk_device():
    """Base block device the system is booted from, e.g. "/dev/mmcblk0" or
    "/dev/sda" - resolved from the root filesystem's own source device
    rather than hardcoded, since which physical media a given Jetson
    boots from (SD card vs eMMC) isn't fixed across units. This is the
    hard safety boundary that keeps this module from ever touching the
    Jetson's own boot media, even though there's no format/mkfs step left
    to actually threaten it - kept anyway as a filter so the Setup page
    never lists the boot disk as a "USB drive" in the first place.
    """
    try:
        result = subprocess.run(
            ["findmnt", "-no", "SOURCE", "/"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    source = result.stdout.strip()
    if not source:
        return None
    # "/dev/mmcblk0p1" -> "/dev/mmcblk0", "/dev/sda1" -> "/dev/sda".
    return re.sub(r"(p)?\d+$", "", source)


def _lsblk_tree():
    try:
        result = subprocess.run(
            # -b for raw byte SIZE values (not lsblk's own locale-formatted
            # "115,3G" strings) - needed to actually compare partition
            # sizes below, not just display them. Reformatted for display
            # by _format_bytes() instead.
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,TRAN,FSTYPE,LABEL,MOUNTPOINT,SIZE"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=5)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"blockdevices": []}


def _format_bytes(byte_count):
    """Same rounding/unit rules as setup.js's own formatBytes() - kept
    visually consistent between the two, even though this one only ever
    formats a whole-disk/partition capacity and that one formats live
    free/used space."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(byte_count)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if value >= 10 or index == 0:
        return "{:.0f} {}".format(value, units[index])
    return "{:.1f} {}".format(value, units[index])


def _device_serial(device_node):
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", device_node],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    properties = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return properties.get("ID_SERIAL_SHORT") or properties.get("ID_SERIAL")


def list_candidates():
    """Every USB disk currently plugged in, shaped for the Setup page's
    picker - one entry per disk, pointing at whichever partition (or the
    disk itself, for an unpartitioned stick) actually carries a
    filesystem, if any."""
    if not _supported:
        return []

    boot_disk = _boot_disk_device()
    registry = _load_registry()
    candidates = []
    for entry in _lsblk_tree().get("blockdevices", []):
        if entry.get("type") != "disk" or entry.get("tran") != "usb":
            continue
        disk_node = "/dev/{}".format(entry.get("name"))
        if boot_disk is not None and disk_node == boot_disk:
            continue

        # Largest filesystem-bearing partition wins, not the first one -
        # real bug hit 2026-09-22: a dual-partition installer stick (a
        # ~200M EFI System Partition + the real, large data partition)
        # picked the tiny ESP every time, since it's always listed first
        # in lsblk's own child order. The whole point of this feature is
        # bulk video storage, so "biggest" is the right tie-breaker - a
        # bare label check (e.g. skip anything named "EFI") would be
        # fragile against a differently-labelled/positioned partition
        # layout, where size never is.
        filesystem_entry = None
        for child in entry.get("children") or []:
            if not child.get("fstype"):
                continue
            # lsblk's own JSON output always quotes SIZE as a string, even
            # with -b - comparing those directly with `>` sorts them
            # lexicographically ("209715200" > "123767619584", since '2' >
            # '1'), so a 200M partition was still beating a 123G one. Real
            # bug hit 2026-09-22, same day as the "largest wins" logic
            # itself: it silently never worked.
            if filesystem_entry is None or int(child.get("size") or 0) > int(filesystem_entry.get("size") or 0):
                filesystem_entry = child
        if filesystem_entry is None and entry.get("fstype"):
            filesystem_entry = entry

        if filesystem_entry is None:
            device_node, fstype, label, mountpoint, size_bytes = disk_node, None, None, None, entry.get("size")
        else:
            device_node = "/dev/{}".format(filesystem_entry.get("name"))
            fstype = filesystem_entry.get("fstype")
            label = filesystem_entry.get("label")
            mountpoint = filesystem_entry.get("mountpoint")
            size_bytes = filesystem_entry.get("size")

        # The Pluto SDR itself enumerates as a small (~30M) USB mass-
        # storage device (label "PlutoSDR", holding its own network
        # config.txt) - it's USB storage by transport, but never a video
        # key, so it's excluded here rather than left for the user to
        # notice and avoid clicking every time.
        if label and label.strip().lower() == "plutosdr":
            continue

        serial = _device_serial(disk_node)
        # Real free-space from the live filesystem (shutil.disk_usage), not
        # lsblk's "size" (the raw partition/disk capacity, ignoring
        # filesystem overhead and what's actually stored on it) - only
        # available once mounted, so the Setup page's gauge just doesn't
        # show until then.
        total_bytes = free_bytes = None
        if mountpoint:
            try:
                usage = shutil.disk_usage(mountpoint)
                total_bytes, free_bytes = usage.total, usage.free
            except OSError:
                pass

        candidates.append({
            "serial": serial,
            "device": device_node,
            "label": label,
            "fstype": fstype,
            # The selected partition's own size, not the whole disk's
            # (real bug alongside the selection one above - "size" used to
            # always report the disk's total capacity regardless of which
            # partition was actually picked, misleadingly showing "115G"
            # even while every other field pointed at the 200M partition).
            "size": _format_bytes(size_bytes) if size_bytes else None,
            "mounted_at": mountpoint,
            "total_bytes": total_bytes,
            "free_bytes": free_bytes,
            "confirmed": bool(serial) and registry.get(serial) == "confirmed",
        })
    return candidates


def select(serial):
    """Called from the Setup page's "use this drive" click - the drive
    must already be mounted (by the OS's own automount) for this to do
    anything; this module never mounts things itself."""
    candidate = next(
        (item for item in list_candidates() if item["serial"] == serial), None)
    if candidate is None:
        return False, "That drive is no longer plugged in"
    if not candidate["mounted_at"]:
        return False, "That drive isn't mounted yet - try unplugging and replugging it"

    try:
        for folder in FOLDER_NAMES_TO_CREATE:
            os.makedirs(os.path.join(candidate["mounted_at"], folder), exist_ok=True)
    except OSError as exc:
        return False, "Could not create folders on that drive: {}".format(exc)

    registry = _load_registry()
    registry[serial] = "confirmed"
    _save_registry(registry)
    return True, None


def mounted_root():
    """Currently-active USB video key's mount path, or None - checked
    fresh via list_candidates() on every call (no cached state to go
    stale), so a previously-confirmed key becomes available again the
    moment the OS re-mounts it after being replugged."""
    for candidate in list_candidates():
        if candidate["confirmed"] and candidate["mounted_at"]:
            return candidate["mounted_at"]
    return None
