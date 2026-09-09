"""Thin command-line adapter for the proven datv_tx_plus.py implementation.

This file contains no replacement GStreamer logic. It supplies web-selected
values to datv_tx_plus.py and then runs that module's original main() unchanged.
"""

import argparse
import os

import datv_tx_plus as tx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--testcard", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--profile", required=True, choices=sorted(tx.PROFILES))
    args = parser.parse_args()

    testcard = os.path.abspath(args.testcard)
    output = os.path.abspath(args.output)
    expected_testcard_dir = os.path.abspath(os.path.join(tx.SCRIPT_DIR, "testcards"))
    if os.path.dirname(testcard) != expected_testcard_dir or not os.path.isfile(testcard):
        raise SystemExit("Invalid testcard path")

    tx.PROFILE = args.profile
    tx.SOURCE = "testcard"
    tx.TX_OUTPUT = "file"
    tx.TX_OUTPUT_FILE = output
    tx.select_testcard_file = lambda: testcard
    tx.main()


if __name__ == "__main__":
    main()
