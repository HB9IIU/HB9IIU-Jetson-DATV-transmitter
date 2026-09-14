"""Small, video-only MJPEG preview for the web interface."""

import subprocess
import threading


PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 10

# Tracks the currently-running preview subprocess (if any), so
# /api/stream/start can stop it before opening the same camera device for
# the real transmission - both hold /dev/videoN open via their own v4l2src,
# and only one process can have it open at a time. A real "Device or
# resource busy" failure was hit 2026-09-13 when a stream was started while
# its own camera's preview was still showing. Guarded by a lock since Flask
# runs threaded=True - a preview request and a stream-start request can
# arrive on different threads at the same time.
_active_preview_lock = threading.Lock()
_active_preview_process = None


def stop_active_preview():
    """Stop whatever preview subprocess is currently running, if any.
    Safe to call even if no preview is active (a no-op then)."""
    global _active_preview_process
    with _active_preview_lock:
        process = _active_preview_process
        _active_preview_process = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _pipeline_for(device, is_csi):
    preview_caps = "video/x-raw,width={},height={},framerate={}/1".format(
        PREVIEW_WIDTH, PREVIEW_HEIGHT, PREVIEW_FPS
    )
    output = [
        "!", "jpegenc", "quality=65",
        "!", "multipartmux", "boundary=frame",
        "!", "fdsink", "fd=1",
    ]

    if is_csi:
        source = [
            "nvarguscamerasrc", "sensor-id=0",
            "!", "video/x-raw(memory:NVMM),width=1280,height=720,framerate=30/1,format=NV12",
            "!", "nvvidconv", "flip-method=2",
            "!", "videorate",
            "!", "video/x-raw,framerate={}/1".format(PREVIEW_FPS),
            "!", "videoscale",
            "!", "video/x-raw,width={},height={}".format(PREVIEW_WIDTH, PREVIEW_HEIGHT),
            "!", "videoconvert",
            "!", "video/x-raw,format=I420",
        ]
    else:
        source = [
            "v4l2src", "device={}".format(device), "do-timestamp=true",
            "!", "videoconvert",
            "!", "videoscale",
            "!", "videorate",
            "!", preview_caps,
        ]
    return ["gst-launch-1.0", "-q"] + source + output


def stream_camera(device, is_csi=False):
    """Yield a multipart JPEG stream and stop GStreamer on disconnect."""
    global _active_preview_process
    # Only one preview is ever meant to be open at a time (a new preview
    # request - e.g. switching cameras - implies the old one is no longer
    # wanted), and this also guarantees a stale reference is never left
    # behind for stop_active_preview() to act on.
    stop_active_preview()
    process = subprocess.Popen(
        _pipeline_for(device, is_csi),
        stdout=subprocess.PIPE,
        stderr=None,
        bufsize=0,
    )
    with _active_preview_lock:
        _active_preview_process = process
    try:
        while True:
            chunk = process.stdout.read(16384)
            if not chunk:
                break
            yield chunk
    finally:
        if process.stdout:
            process.stdout.close()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        with _active_preview_lock:
            if _active_preview_process is process:
                _active_preview_process = None
