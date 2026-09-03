#!/usr/bin/env python3
"""Generate the deterministic Stage-0 DATV scrolling-text reference movie."""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


APP_DIR = Path(__file__).resolve().parent
DEFAULT_BACKGROUND = APP_DIR / "generated-assets" / "datv-reference-background-v1.png"
DEFAULT_OUTPUT = APP_DIR / "generated-assets" / "datv-scroll-reference-720p25-v1.mkv"
DEFAULT_PREVIEW = APP_DIR / "generated-assets" / "datv-scroll-reference-preview-v1.png"
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
)


def font(size):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError("No suitable TrueType font found")


def rounded_panel(image, box, radius=16, fill=(3, 12, 23, 196), outline=(58, 196, 231, 145)):
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)
    return Image.alpha_composite(image, overlay)


def fit_background(path):
    image = Image.open(path).convert("RGB")
    target_ratio = 1280.0 / 720.0
    ratio = image.width / float(image.height)
    if ratio > target_ratio:
        crop_width = int(round(image.height * target_ratio))
        left = (image.width - crop_width) // 2
        image = image.crop((left, 0, left + crop_width, image.height))
    elif ratio < target_ratio:
        crop_height = int(round(image.width / target_ratio))
        top = (image.height - crop_height) // 2
        image = image.crop((0, top, image.width, top + crop_height))
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize((1280, 720), resampling).convert("RGBA")


def compose_frame(background, frame_number, fps, ticker_speed, message, callsign,
                  motion_samples=1):
    image = background.copy()
    image = rounded_panel(image, (40, 34, 495, 158))
    image = rounded_panel(image, (935, 34, 1240, 158))

    ticker_overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ticker_draw = ImageDraw.Draw(ticker_overlay)
    ticker_draw.rectangle((0, 620, 1280, 720), fill=(2, 9, 18, 222))
    ticker_draw.line((0, 620, 1280, 620), fill=(70, 205, 237, 210), width=2)
    image = Image.alpha_composite(image, ticker_overlay)
    draw = ImageDraw.Draw(image)

    white = (238, 247, 255, 255)
    cyan = (75, 205, 235, 255)
    muted = (160, 186, 205, 255)
    draw.text((62, 48), callsign, font=font(42), fill=white)
    draw.text((64, 101), "DATV ENCODING REFERENCE", font=font(19), fill=cyan)
    draw.text((64, 130), "REF: SCROLL-720P{}-V1".format(fps), font=font(14), fill=muted)

    clock = datetime(2026, 8, 10, 12, 0, 0) + timedelta(seconds=frame_number / float(fps))
    draw.text((961, 50), clock.strftime("UTC %H:%M:%S"), font=font(28), fill=white)
    draw.text((963, 101), clock.strftime("%d %b %Y").upper(), font=font(17), fill=cyan)
    draw.text((963, 130), "1280x720 / {} FPS".format(fps), font=font(13), fill=muted)

    ticker_font = font(34)
    bbox = draw.textbbox((0, 0), message, font=ticker_font)
    text_width = bbox[2] - bbox[0]
    cycle = 1280 + text_width + 240
    if motion_samples == 1:
        travelled = (frame_number * ticker_speed) / float(fps)
        x = 1280 - (travelled % cycle)
        draw.text((int(round(x)), 644), message, font=ticker_font, fill=white)
    else:
        # Blend positions across the previous frame interval. This approximates
        # a camera shutter and prevents perfectly sharp CGI text from strobing.
        target_opacity = 0.92
        sample_alpha = int(round(255 * (1 - (1 - target_opacity) ** (1.0 / motion_samples))))
        blur = Image.new("RGBA", image.size, (0, 0, 0, 0))
        for sample in range(motion_samples):
            offset = sample / float(motion_samples - 1)
            travelled = ((frame_number - offset) * ticker_speed) / float(fps)
            x = 1280 - (travelled % cycle)
            layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).text(
                (int(round(x)), 644), message, font=ticker_font,
                fill=(white[0], white[1], white[2], sample_alpha),
            )
            blur = Image.alpha_composite(blur, layer)
        image = Image.alpha_composite(image, blur)
    return image.convert("RGB")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", type=Path, default=DEFAULT_BACKGROUND)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--ticker-speed", type=float, default=75.0)
    parser.add_argument("--motion-samples", type=int, default=1,
                        help="Blend this many ticker positions per output frame")
    parser.add_argument("--callsign", default="HB9IIU")
    parser.add_argument("--message", default="HB9IIU  •  JN36KL  •  QO-100 DATV  •  SCROLLING TEXT AND COMPRESSION REFERENCE  •  0123456789  •  73")
    parser.add_argument("--preview", type=Path, help="Write one representative PNG instead of a movie")
    parser.add_argument("--raw-stdout", action="store_true",
                        help="Write raw RGB24 frames to stdout instead of creating a movie")
    parser.add_argument("--tail-frames", type=int, default=0,
                        help="Append duplicates of the final frame (useful for encoder drain)")
    return parser.parse_args()


def main():
    args = arguments()
    if (args.fps <= 0 or args.duration <= 0 or args.ticker_speed <= 0 or
            args.tail_frames < 0 or args.motion_samples <= 0 or args.motion_samples == 2):
        raise SystemExit("fps, duration and ticker speed must be positive; motion samples must be 1 or at least 3")
    background = fit_background(args.background)
    if args.preview:
        args.preview.parent.mkdir(parents=True, exist_ok=True)
        frame_number = int(args.fps * 8)
        compose_frame(background, frame_number, args.fps, args.ticker_speed,
                      args.message, args.callsign, args.motion_samples).save(args.preview)
        print(args.preview)
        return

    frame_count = int(round(args.duration * args.fps))
    if args.raw_stdout:
        output = sys.stdout.buffer
        for frame_number in range(frame_count + args.tail_frames):
            source_frame = min(frame_number, frame_count - 1)
            frame = compose_frame(background, source_frame, args.fps,
                                  args.ticker_speed, args.message, args.callsign,
                                  args.motion_samples)
            output.write(frame.tobytes())
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1280x720",
        "-r", str(args.fps), "-i", "-", "-an", "-c:v", "ffv1",
        "-level", "3", "-pix_fmt", "bgr0", "-r", str(args.fps),
        str(args.output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for frame_number in range(frame_count + args.tail_frames):
            source_frame = min(frame_number, frame_count - 1)
            frame = compose_frame(background, source_frame, args.fps,
                                  args.ticker_speed, args.message, args.callsign,
                                  args.motion_samples)
            process.stdin.write(frame.tobytes())
    finally:
        if process.stdin:
            process.stdin.close()
    if process.wait() != 0:
        raise SystemExit("FFmpeg failed while writing the reference movie")
    print(args.output)


if __name__ == "__main__":
    main()
