#!/usr/bin/env python3
"""Compare Stage-1 encodes with the lossless DATV reference."""

import json
import re
import subprocess
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ASSET_DIR = APP_DIR / "generated-assets"
REFERENCE = ASSET_DIR / "datv-scroll-reference-720p25-v1.mkv"
ENCODES = {
    "720p": ASSET_DIR / "stage1-720p25-h265-500k-gop50-final.mkv",
    "540p": ASSET_DIR / "stage1-540p25-h265-500k-gop50-final.mkv",
}
REPORT = ASSET_DIR / "stage1-500k-quality-report.json"


def measure(encoded, ticker_only=False):
    crop = ",crop=1280:100:0:620" if ticker_only else ""
    graph = (
        "[0:v]scale=1280:720:flags=lanczos,format=yuv420p,setpts=PTS-STARTPTS"
        + crop + ",split=2[test_ssim][test_psnr];"
        "[1:v]format=yuv420p,setpts=PTS-STARTPTS"
        + crop + ",split=2[ref_ssim][ref_psnr];"
        "[test_ssim][ref_ssim]ssim[ssim_out];"
        "[test_psnr][ref_psnr]psnr[psnr_out]"
    )
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(encoded),
        "-i", str(REFERENCE), "-filter_complex", graph,
        "-map", "[ssim_out]", "-map", "[psnr_out]", "-f", "null", "-",
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    ssim = re.findall(r"SSIM.*All:([0-9.]+)", result.stderr)
    psnr = re.findall(r"PSNR.*average:([0-9.]+)", result.stderr)
    if not ssim or not psnr:
        raise RuntimeError("FFmpeg did not return SSIM/PSNR summaries")
    return {"ssim": float(ssim[-1]), "psnr_db": float(psnr[-1])}


def main():
    report = {}
    for name, encoded in ENCODES.items():
        print("Analysing {} full frame...".format(name), flush=True)
        full = measure(encoded)
        print("Analysing {} scrolling banner...".format(name), flush=True)
        ticker = measure(encoded, ticker_only=True)
        report[name] = {"full_frame": full, "scrolling_banner": ticker}
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(REPORT)


if __name__ == "__main__":
    main()
