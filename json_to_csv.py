"""
Convert extracted_streets.output.json to a CSV file for the multiskiptrace app.

Usage:
    python json_to_csv.py
    python json_to_csv.py --input results/extracted_streets.output.json --output results/addresses_for_skiptrace.csv

The output CSV is formatted with the columns expected by the multiskiptrace app:
    Address, City, State, Zip, County
"""

import json
import csv
import argparse
from pathlib import Path

DEFAULT_INPUT = Path(__file__).parent / "results" / "extracted_streets.output.json"
DEFAULT_OUTPUT = Path(__file__).parent / "results" / "addresses_for_skiptrace.csv"


def convert(input_path: Path, output_path: Path) -> int:
    with open(input_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    fieldnames = ["Address", "City", "State", "Zip", "County"]
    written = 0

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for record in records:
            address = record.get("fullAddress") or record.get("streetName", "")
            house = record.get("houseNumber", "")
            if house and address and not address.startswith(str(house)):
                address = f"{house} {address}"

            row = {
                "Address": address.strip(),
                "City": record.get("city", ""),
                "State": record.get("state", ""),
                "Zip": record.get("zip", ""),
                "County": record.get("county", ""),
            }

            # Skip records that have no usable address
            if not row["Address"]:
                continue

            writer.writerow(row)
            written += 1

    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert JSON addresses to CSV for multiskiptrace")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Path to the .output.json file")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to write the CSV file")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}")
        raise SystemExit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    count = convert(input_path, output_path)
    print(f"Done! Wrote {count} addresses to: {output_path}")
    print(f"\nNext steps:")
    print(f"  1. Open the multiskiptrace app at http://127.0.0.1:5001/")
    print(f"  2. Upload: {output_path}")
