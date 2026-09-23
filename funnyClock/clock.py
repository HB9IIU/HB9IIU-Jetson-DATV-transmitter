#!/usr/bin/env python3
import math
import subprocess
import time
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1280, 720
FPS = 25
DURATION = 70                 # seconds; includes a full sweep and pause
OUTPUT = "sbb_clock.mp4"

CALLSIGN = "HB9IIU"
LOCATOR = "JN36kl"

CARD_GREY = "#6e6e6e"
RIM_COLOR = "#b4b8bc"
SBB_RED = "#e02020"

cx, cy = WIDTH // 2, HEIGHT // 2
radius = 300                  # white dial, in output pixels
RIM = 12                      # silver bezel width, in output pixels
SS = 3                        # supersampling factor for smooth edges

GRID = 60                     # test card grid pitch, in output pixels
EDGE = 20                     # height of the black/white edge blocks

# 75% colour bars, top to bottom.
BARS = [
    (191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
    (191, 0, 191), (191, 0, 0), (0, 0, 191), (0, 0, 0),
]
GREY_STEPS = [255, 204, 153, 102, 51, 0]
GRATING_WIDTHS = [4, 3, 2, 1]  # line width per row, coarse to fine

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Jetson
    "C:/Windows/Fonts/arialbd.ttf",
]

# The dial is drawn SS times larger in its own square box, then shrunk
# down and pasted onto the frame - that gives anti-aliased edges.
half = radius + RIM + 4
BOX = 2 * half * SS
R = radius * SS
C = BOX / 2

ffmpeg = subprocess.Popen(
    [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{WIDTH}x{HEIGHT}",
        "-framerate", str(FPS),
        "-i", "-",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        OUTPUT,
    ],
    stdin=subprocess.PIPE,
)

def point(angle, dist):
    return (C + math.sin(angle) * dist, C - math.cos(angle) * dist)

def bar(draw, angle, r_from, r_to, w_from, w_to, fill):
    # Flat-ended bar along `angle`, tapering from w_from to w_to.
    # All sizes are fractions of the dial radius; negative r_from is a tail.
    px, py = math.cos(angle), math.sin(angle)
    x0, y0 = point(angle, r_from * R)
    x1, y1 = point(angle, r_to * R)
    a, b = w_from * R / 2, w_to * R / 2
    draw.polygon([
        (x0 - px * a, y0 - py * a),
        (x1 - px * b, y1 - py * b),
        (x1 + px * b, y1 + py * b),
        (x0 + px * a, y0 + py * a),
    ], fill=fill)

def disc(draw, x, y, r, fill):
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)

def load_font(size):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()

def centered_text(draw, x, y, text, font, fill):
    if hasattr(font, "getbbox"):
        left, top, right, bottom = font.getbbox(text)
    else:  # Pillow < 8 (Jetson)
        left, top = font.getoffset(text)
        right, bottom = font.getsize(text)
    draw.text((x - (left + right) / 2, y - (top + bottom) / 2),
              text, font=font, fill=fill)

def build_testcard():
    # PM5544-style card: grey with a white grid, edge blocks, colour bars
    # on the left, grey steps and frequency gratings on the right.
    card = Image.new("RGB", (WIDTH, HEIGHT), CARD_GREY)
    draw = ImageDraw.Draw(card)

    for x in range(cx % GRID, WIDTH, GRID):
        draw.rectangle((x - 1, 0, x, HEIGHT), fill="white")
    for y in range(cy % GRID, HEIGHT + 1, GRID):
        draw.rectangle((0, y - 1, WIDTH, y), fill="white")

    for i, x in enumerate(range(cx % GRID - GRID, WIDTH, GRID)):
        fill = "black" if i % 2 else "white"
        draw.rectangle((x, 0, x + GRID - 1, EDGE - 1), fill=fill)
        draw.rectangle((x, HEIGHT - EDGE, x + GRID - 1, HEIGHT - 1),
                       fill=fill)

    top, bottom = cy - 4 * GRID, cy + 4 * GRID
    left_x0, left_x1 = cx - 10 * GRID, cx - 6 * GRID
    right_x0, right_x1 = cx + 6 * GRID, cx + 10 * GRID

    # Black frame behind each panel, the panel contents cover the inside.
    for x0, x1 in ((left_x0, left_x1), (right_x0, right_x1)):
        draw.rectangle((x0 - 2, top - 2, x1 + 1, bottom + 1), fill="black")

    for i, colour in enumerate(BARS):
        y = top + i * GRID
        draw.rectangle((left_x0, y, left_x1 - 1, y + GRID - 1), fill=colour)

    step = (right_x1 - right_x0) // len(GREY_STEPS)
    for i, level in enumerate(GREY_STEPS):
        x = right_x0 + i * step
        draw.rectangle((x, top, x + step - 1, cy - 1),
                       fill=(level, level, level))

    for row, width in enumerate(GRATING_WIDTHS):
        y = cy + row * GRID
        for x in range(right_x0, right_x1, 2 * width):
            draw.rectangle((x, y, x + width - 1, y + GRID - 1), fill="white")

    return card

background = build_testcard()

# Static parts (bezel, dial, markers, callsign) are drawn once, on top of
# the matching piece of the test card so the box corners blend in.
face = background.crop((cx - half, cy - half, cx + half, cy + half))
face = face.resize((BOX, BOX), Image.NEAREST)
draw = ImageDraw.Draw(face)
disc(draw, C, C, R + (RIM + 3) * SS, "black")
disc(draw, C, C, R + RIM * SS, RIM_COLOR)
disc(draw, C, C, R, "white")

for i in range(60):
    angle = 2 * math.pi * i / 60
    if i % 5 == 0:
        bar(draw, angle, 0.75, 0.97, 0.065, 0.065, "black")
    else:
        bar(draw, angle, 0.90, 0.97, 0.024, 0.024, "black")

centered_text(draw, C, C - 0.45 * R, CALLSIGN, load_font(64 * SS), "black")
centered_text(draw, C, C + 0.40 * R, LOCATOR, load_font(40 * SS), "black")

try:
    start = time.monotonic()

    for frame_number in range(DURATION * FPS):
        # Use wall-clock time so the displayed clock shows the actual time.
        now = time.time()
        local = time.localtime(now)
        second = (now % 60)

        dial = face.copy()
        draw = ImageDraw.Draw(dial)

        hour_angle = 2 * math.pi * (
            (local.tm_hour % 12 + local.tm_min / 60) / 12
        )
        minute_angle = 2 * math.pi * local.tm_min / 60

        # SBB second hand reaches 12 after 58.5 seconds, then waits.
        second_angle = 2 * math.pi * min(second / 58.5, 1.0)

        # Tapered black hands with a short tail past the centre.
        bar(draw, hour_angle, -0.20, 0.64, 0.12, 0.095, "black")
        bar(draw, minute_angle, -0.20, 0.90, 0.095, 0.07, "black")

        # Thin red rod with a long tail, big disc about 60% out.
        bar(draw, second_angle, -0.30, 0.60, 0.022, 0.022, SBB_RED)
        disc(draw, *point(second_angle, 0.60 * R), 0.10 * R, SBB_RED)

        image = background.copy()
        # resize+BOX instead of reduce(): the Jetson's Pillow is older than 7.0.
        small = dial.resize((2 * half, 2 * half), Image.BOX)
        image.paste(small, (cx - half, cy - half))

        ffmpeg.stdin.write(image.tobytes())

        next_frame = start + (frame_number + 1) / FPS
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)

finally:
    ffmpeg.stdin.close()
    ffmpeg.wait()

print(f"Saved {OUTPUT}")
