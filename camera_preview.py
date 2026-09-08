"""Small, video-only MJPEG preview for the web interface."""

import subprocess


PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 10


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
    process = subprocess.Popen(
        _pipeline_for(device, is_csi),
        stdout=subprocess.PIPE,
        stderr=None,
        bufsize=0,
    )
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
