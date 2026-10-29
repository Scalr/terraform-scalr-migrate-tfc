"""CSV and TSV files: the delimiter follows the file extension."""
import csv
import os
from typing import Dict, List

from scalr_tfc_migrate.constants import INVENTORY_FORMAT_TSV
from scalr_tfc_migrate.errors import InvalidInputError


def delimiter_for(path: str) -> str:
    return "\t" if path.lower().endswith(f".{INVENTORY_FORMAT_TSV}") else ","


def write_table(path: str, columns: List[str], rows: List[Dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore", delimiter=delimiter_for(path))
        writer.writeheader()
        writer.writerows(rows)


def read_table(path: str, required_columns: List[str]) -> List[Dict[str, str]]:
    """
    Rows of a CSV or TSV file with a header line, keyed by lower-cased column name. Values are
    stripped, and rows that are empty or start with "#" are skipped.
    """
    if not os.path.exists(path):
        raise InvalidInputError(f"File '{path}' does not exist")

    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter_for(path))
        columns = [column.strip().lower() for column in reader.fieldnames or []]
        missing = [column for column in required_columns if column not in columns]
        if missing:
            raise InvalidInputError(f"File '{path}' has no column(s): {', '.join(missing)}")

        rows = []
        for raw in reader:
            row = {
                (key or "").strip().lower(): (value or "").strip() if isinstance(value, str) else ""
                for key, value in raw.items()
            }
            values = [value for value in row.values() if value]
            if not values or values[0].startswith("#"):
                continue
            rows.append(row)
    return rows
