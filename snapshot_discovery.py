"""
Snapshot Discovery Module

Finds, parses, and sorts zip file snapshots for a given project name.
Handles multiple naming conventions:
  - YYYYMMDD[letter]: Document_Analyzer_20250923b.zip
  - YYMMDD: Document_Analyzer_250507.zip
  - YY-MM-DD or YY_MM_DD: Accessibility_Shortcuts_22-08-01.zip
  - MM-DD-YY or MM_DD_YY: Arduino_sketches_02-27-21.zip
  - M-DD-YY (no zero-pad): Arduino_sketches_8-14-21.zip
  - YYYYMMDD_N: Mentorship_Database_20250909_1.zip
  - NNNN (incremental): BrushTest_0001.zip
  - N.N (version): SimpleCCompiler_0.1.zip
  - vN (version): Media_Display_v1.zip
"""

import os
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class SnapshotInfo:
    """Information about a single snapshot zip file."""
    path: str           # full path to zip file
    sort_key: tuple     # sortable tuple for ordering
    label: str          # human-readable label (e.g., "20250923b" or "0035")
    filename: str       # just the filename


def _parse_suffix(suffix: str) -> Optional[tuple]:
    """
    Parse the suffix portion of a zip filename into a sortable key.

    Returns a tuple (type_order, type_tag, *values) where type_order is an int
    that controls cross-type sorting (ver < seq < date) and type_tag is a
    string label for display/debugging.

    Returns None if the suffix cannot be parsed.
    """

    # Pattern 1: YYYYMMDD with optional letter suffix and optional _N sub-suffix
    # e.g., "20250923", "20250923b", "20250909_1"
    m = re.match(r'^(\d{4})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])([a-z]?)(?:_(\d+))?$', suffix)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        letter = m.group(4) if m.group(4) else ''
        sub_num = int(m.group(5)) if m.group(5) else 0
        letter_ord = ord(letter) - ord('a') + 1 if letter else 0
        return (2, 'date', year, month, day, letter_ord, sub_num)

    # Pattern 2: YYMMDD (6-digit compact date)
    # e.g., "250507", "250601"
    m = re.match(r'^(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$', suffix)
    if m:
        year = int(m.group(1)) + 2000
        month, day = int(m.group(2)), int(m.group(3))
        return (2, 'date', year, month, day, 0, 0)

    # Pattern 3: Date with separators (dash or underscore)
    # Handles: YY-MM-DD, MM-DD-YY, M-D-YY, and underscore variants
    m = re.match(r'^(\d{1,2})[-_](\d{1,2})[-_](\d{2,4})$', suffix)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))

        # If third part is 4 digits, it's MM-DD-YYYY or DD-MM-YYYY
        if c >= 100:
            year = c
            # Assume MM-DD-YYYY (US format) since all examples use this
            month, day = a, b
        else:
            # Two-digit year in third position: MM-DD-YY or M-D-YY
            # Two-digit year in first position: YY-MM-DD
            # Disambiguation: if first segment > 12, it can't be a month,
            # so it must be YY. If first segment <= 12, check if third
            # segment could be a valid year (it's always 2 digits here).
            # Convention from the real data:
            #   - 22-08-01 is YY-MM-DD (year 2022, Aug 1)
            #   - 02-27-21 is MM-DD-YY (Feb 27, 2021)
            #   - 8-14-21 is M-DD-YY (Aug 14, 2021)
            # Heuristic: if a > 12, it's definitely YY-MM-DD.
            # If a <= 12 and c <= 31 and b <= 31, check if it makes
            # more sense as MM-DD-YY. If a <= 12 and b > 12, then
            # a must be month (MM-DD-YY). If a <= 12 and b <= 12,
            # ambiguous - we use file context but default to MM-DD-YY
            # when c looks like a recent 2-digit year (18-26).
            year_c = c + 2000 if c < 100 else c

            if a > 12:
                # a can't be a month, must be YY
                year = a + 2000 if a < 100 else a
                month, day = b, c
                # But c is the day in this case, and we already set year
                # Recalculate: YY-MM-DD
                year = a + 2000
                month, day = b, c
            else:
                # a <= 12: could be month (MM-DD-YY) or year (YY-MM-DD)
                # In the real data, when a <= 12 and the format uses
                # dashes, the recent files (2022+) use YY-MM-DD while
                # older files use MM-DD-YY.
                # Best heuristic: if b > 12, then b must be a day and
                # a is month -> MM-DD-YY.
                # If b <= 12, it's ambiguous. Use the convention that
                # if c is in range 13-31, it's YY-MM-DD (c is the day).
                # If c is in range 1-12, still ambiguous. Fall back to
                # MM-DD-YY as the US convention.
                if b > 12:
                    # b is definitely a day, a is month
                    month, day = a, b
                    year = year_c
                elif c > 23:
                    # c > 23 is too high for a 2-digit year in our range,
                    # so c is a day -> YY-MM-DD
                    year = a + 2000
                    month, day = b, c
                else:
                    # Truly ambiguous. Default to MM-DD-YY (US convention).
                    month, day = a, b
                    year = year_c

        # Validate
        if not (1 <= month <= 12 and 1 <= day <= 31 and 2000 <= year <= 2099):
            return None
        return (2, 'date', year, month, day, 0, 0)

    # Pattern 4: Pure incremental number (3+ digits to distinguish from dates)
    # e.g., "0001", "0235", "0057"
    m = re.match(r'^(\d{3,})$', suffix)
    if m:
        return (1, 'seq', int(m.group(1)))

    # Pattern 5: Version with dot notation
    # e.g., "0.1", "0.2", "1.0", "2.3.1"
    m = re.match(r'^(\d+(?:\.\d+)+)$', suffix)
    if m:
        parts = tuple(int(x) for x in m.group(1).split('.'))
        return (0, 'ver') + parts

    # Pattern 6: Version with 'v' prefix
    # e.g., "v1", "v2", "v10"
    m = re.match(r'^v(\d+)$', suffix, re.IGNORECASE)
    if m:
        return (0, 'ver', int(m.group(1)))

    return None


def _extract_project_and_suffix(filename: str, project_name: str) -> Optional[str]:
    """
    Given a filename and expected project name, extract the version/date suffix.

    Matching is case-insensitive on the project name.
    The filename must be: {project_name}_{suffix}.zip

    Returns the suffix string, or None if the filename doesn't match.
    """
    # Remove .zip extension
    if not filename.lower().endswith('.zip'):
        return None
    stem = filename[:-4]

    # Check if stem starts with project_name (case-insensitive) followed by _
    if len(stem) <= len(project_name) + 1:
        return None

    name_part = stem[:len(project_name)]
    separator = stem[len(project_name)]

    if name_part.lower() != project_name.lower():
        return None
    if separator != '_':
        return None

    suffix = stem[len(project_name) + 1:]
    if not suffix:
        return None

    return suffix


def discover_snapshots(zip_directory: str, project_name: str) -> list[SnapshotInfo]:
    """
    Find and sort all zip snapshots for a given project name.

    Args:
        zip_directory: Path to directory containing zip files
        project_name: Project name to match (e.g., "Document_Analyzer")

    Returns:
        Sorted list of SnapshotInfo objects

    Raises:
        FileNotFoundError: If zip_directory doesn't exist
        ValueError: If fewer than 2 matching snapshots found
        ValueError: If any matching filename has an unparseable suffix
    """
    if not os.path.isdir(zip_directory):
        raise FileNotFoundError(f"Zip directory not found: {zip_directory}")

    snapshots = []
    unparseable = []

    for filename in os.listdir(zip_directory):
        suffix = _extract_project_and_suffix(filename, project_name)
        if suffix is None:
            continue

        sort_key = _parse_suffix(suffix)
        if sort_key is None:
            unparseable.append(filename)
            continue

        full_path = os.path.join(zip_directory, filename)
        if not os.path.isfile(full_path):
            continue

        snapshots.append(SnapshotInfo(
            path=full_path,
            sort_key=sort_key,
            label=suffix,
            filename=filename
        ))

    if unparseable:
        raise ValueError(
            f"Found {len(unparseable)} matching zip file(s) with unparseable suffixes:\n"
            + "\n".join(f"  {f}" for f in unparseable)
        )

    if len(snapshots) < 2:
        raise ValueError(
            f"Need at least 2 snapshots for project '{project_name}', "
            f"found {len(snapshots)} in {zip_directory}"
        )

    # Sort by sort_key
    snapshots.sort(key=lambda s: s.sort_key)

    return snapshots


def list_projects(zip_directory: str) -> dict[str, int]:
    """
    Scan the zip directory and list all detected project names with snapshot counts.

    Returns a dict of {project_name: count}, sorted by name.
    Only includes projects with 2+ snapshots.
    """
    if not os.path.isdir(zip_directory):
        raise FileNotFoundError(f"Zip directory not found: {zip_directory}")

    # Group filenames by potential project name
    project_counts: dict[str, int] = {}

    for filename in os.listdir(zip_directory):
        if not filename.lower().endswith('.zip'):
            continue
        stem = filename[:-4]

        # Find the last underscore that separates project name from suffix
        # Try progressively shorter prefixes until we find a parseable suffix
        last_idx = len(stem)
        while True:
            idx = stem.rfind('_', 0, last_idx)
            if idx <= 0:
                break
            candidate_name = stem[:idx]
            candidate_suffix = stem[idx + 1:]
            if _parse_suffix(candidate_suffix) is not None:
                name_lower = candidate_name.lower()
                project_counts[name_lower] = project_counts.get(name_lower, 0) + 1
                break
            last_idx = idx

    # Filter to 2+ snapshots and sort
    return dict(sorted(
        ((name, count) for name, count in project_counts.items() if count >= 2),
        key=lambda x: x[0]
    ))


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python snapshot_discovery.py <zip_directory> [project_name]")
        print("  If project_name is omitted, lists all projects found.")
        sys.exit(1)

    zip_dir = sys.argv[1]

    if len(sys.argv) >= 3:
        proj_name = sys.argv[2]
        try:
            snaps = discover_snapshots(zip_dir, proj_name)
            print(f"Found {len(snaps)} snapshots for '{proj_name}':")
            for s in snaps:
                print(f"  {s.label:20s}  {s.sort_key}")
        except (ValueError, FileNotFoundError) as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        projects = list_projects(zip_dir)
        if not projects:
            print("No projects with 2+ snapshots found.")
        else:
            print(f"Found {len(projects)} projects with 2+ snapshots:")
            for name, count in projects.items():
                print(f"  {name:30s}  {count} snapshots")
