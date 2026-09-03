#!/bin/sh
set -eu

SOURCE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/testcards" && pwd)"
OUTPUT_DIR="$SOURCE_DIR/normalized"
mkdir -p "$OUTPUT_DIR"

normalize() {
    input="$1"
    output="$2"
    ffmpeg -hide_banner -loglevel error -y \
        -i "$SOURCE_DIR/$input" \
        -vf "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1" \
        -frames:v 1 "$OUTPUT_DIR/$output"
}

normalize "BBC-Test-Card-F.png" "bbc-test-card-f.png"
normalize "COLOR-Test-Card.png" "color-test-card.png"
normalize "Ladies V.2 UHD JPG.jpg" "ladies-uhd.png"
normalize "Neutral tescard.jpg" "neutral-test-card.png"
normalize "RCA_Indian_Head_Test_Pattern.png" "rca-indian-head.png"
normalize "TV-test-pattern1 GREY.png" "tv-test-pattern-grey.png"
normalize "ladies colour.jpeg" "ladies-color.png"
normalize "ladies.png" "ladies-reference.png"
normalize "mire_0.jpg" "hb9iiu-portrait.png"
normalize "mire_1.jpg" "mire-01.png"
normalize "mire_2.jpg" "mire-02.png"
normalize "mire_3.jpg" "mire-03.png"
normalize "mire_4.jpg" "mire-04.png"
normalize "mire_5.jpg" "mire-05.png"
normalize "mire_6.jpg" "mire-06.png"
normalize "mire_7.jpg" "mire-07.png"
normalize "testcardHB9.jpg" "hb9iiu-test-card.png"

printf 'Normalized test cards written to %s\n' "$OUTPUT_DIR"
