"""
Snapshot Diff Module

Extracts consecutive zip snapshots to temporary directories and computes
detailed diffs: files added, removed, modified, moved, and unchanged.
Detects status documents within snapshots for contextual analysis.
"""

import os
import hashlib
import difflib
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Optional


# Default binary extensions if not provided via config
DEFAULT_BINARY_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg',
    '.exe', '.dll', '.so', '.dylib', '.bin',
    '.zip', '.gz', '.tar', '.rar', '.7z',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.pyc', '.pyo', '.class', '.o', '.obj',
    '.db', '.sqlite', '.sqlite3',
    '.mp3', '.mp4', '.wav', '.avi', '.mov',
    '.ttf', '.otf', '.woff', '.woff2',
    '.ds_store',
    '.suo', '.cache', '.resources', '.pdb',
    '.nupkg', '.snk',
}

# Known status/documentation filenames (lowercase for matching)
STATUS_DOC_NAMES = {
    'status.md', 'changelog.md', 'todo.md', 'notes.md', 'readme.md',
    'development.md', 'devlog.md', 'history.md', 'claude.md', 'progress.md',
    'release_notes.md', 'roadmap.md', 'lessons_learned.md',
}

# Patterns for status doc detection (checked against basename, lowercase)
STATUS_DOC_PREFIXES = ('devlog', 'changelog', 'release_notes', 'todo')


@dataclass
class FileDiff:
    """A modified file with its unified diff."""
    path: str
    diff_text: str      # unified diff as a single string
    diff_line_count: int


@dataclass
class SnapshotDiff:
    """Complete diff between two snapshots."""
    added: list[str]                                # paths of new files
    removed: list[str]                              # paths of deleted files
    modified: list[FileDiff]                        # changed files with diffs
    moved: list[tuple[str, str]]                    # (old_path, new_path)
    unchanged: list[str]                            # paths unchanged
    total_diff_lines: int                           # sum of all diff lines
    files_changed_count: int                        # added + removed + modified + moved
    new_file_listing: list[str]                     # all non-binary files in new snapshot
    old_file_listing: list[str]                     # all non-binary files in old snapshot
    total_lines_in_new: int                         # total lines across all files in new snapshot
    status_docs: dict[str, str] = field(default_factory=dict)
    status_doc_diffs: list[FileDiff] = field(default_factory=list)


def _is_binary(filepath: str, binary_extensions: set[str]) -> bool:
    """Check if a file should be treated as binary based on extension."""
    _, ext = os.path.splitext(filepath)
    if ext.lower() in binary_extensions:
        return True
    # Also skip files with no extension that live in known binary directories
    basename = os.path.basename(filepath).lower()
    if basename in {'thumbs.db', 'desktop.ini', '.gitattributes'}:
        return False  # These are text
    return False


def _is_status_doc(filepath: str) -> bool:
    """Check if a file is a status/documentation document."""
    basename = os.path.basename(filepath).lower()
    if basename in STATUS_DOC_NAMES:
        return True
    for prefix in STATUS_DOC_PREFIXES:
        if basename.startswith(prefix):
            return True
    return False


def _file_hash(filepath: str) -> str:
    """Compute SHA256 hash of a file's contents."""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def _read_text_file(filepath: str) -> Optional[list[str]]:
    """Read a text file as lines. Returns None if the file can't be decoded."""
    for encoding in ('utf-8', 'latin-1'):
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                return f.readlines()
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _count_lines(filepath: str) -> int:
    """Count lines in a text file."""
    lines = _read_text_file(filepath)
    return len(lines) if lines is not None else 0


def _walk_files(root_dir: str, binary_extensions: set[str]) -> dict[str, str]:
    """
    Walk a directory tree and return {relative_path: absolute_path}
    for all non-binary files.
    """
    files = {}
    for dirpath, _, filenames in os.walk(root_dir):
        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, root_dir).replace('\\', '/')
            if not _is_binary(rel_path, binary_extensions):
                files[rel_path] = abs_path
    return files


def _find_root_dir(extract_dir: str) -> str:
    """
    Find the effective root directory after extraction.

    Many zip files contain a single top-level directory that wraps all content.
    This function detects that pattern and returns the inner directory,
    so that file paths are relative to the actual project root.
    """
    entries = os.listdir(extract_dir)
    # Filter out common junk entries
    entries = [e for e in entries if not e.startswith('.') and e != '__MACOSX']

    if len(entries) == 1:
        single = os.path.join(extract_dir, entries[0])
        if os.path.isdir(single):
            return single

    return extract_dir


def _compute_diff(old_path: str, new_path: str, rel_path: str,
                  max_lines: int = 0) -> Optional[FileDiff]:
    """
    Compute unified diff between two files.

    Args:
        old_path: Path to old version of the file
        new_path: Path to new version of the file
        rel_path: Relative path for display
        max_lines: Maximum diff lines to include (0 = unlimited)

    Returns:
        FileDiff if files differ, None if identical or unreadable
    """
    old_lines = _read_text_file(old_path)
    new_lines = _read_text_file(new_path)

    if old_lines is None or new_lines is None:
        return None

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
        lineterm=''
    ))

    if not diff_lines:
        return None

    # Strip trailing newlines from each diff line for clean output
    diff_lines = [line.rstrip('\n').rstrip('\r') for line in diff_lines]

    if max_lines > 0 and len(diff_lines) > max_lines:
        truncated = len(diff_lines) - max_lines
        diff_lines = diff_lines[:max_lines]
        diff_lines.append(f"\n... ({truncated} more lines truncated)")

    diff_text = '\n'.join(diff_lines)
    return FileDiff(
        path=rel_path,
        diff_text=diff_text,
        diff_line_count=len(diff_lines)
    )


def diff_snapshots(old_zip: str, new_zip: str,
                   binary_extensions: Optional[list[str]] = None,
                   max_diff_lines: int = 0) -> SnapshotDiff:
    """
    Extract and diff two zip snapshots.

    Args:
        old_zip: Path to the older zip file
        new_zip: Path to the newer zip file
        binary_extensions: List of extensions to skip (e.g., ['.png', '.exe']).
                          Uses DEFAULT_BINARY_EXTENSIONS if None.
        max_diff_lines: Maximum diff lines per file (0 = unlimited)

    Returns:
        SnapshotDiff with all change information

    Raises:
        FileNotFoundError: If either zip file doesn't exist
        zipfile.BadZipFile: If either zip is corrupt
    """
    if not os.path.isfile(old_zip):
        raise FileNotFoundError(f"Old zip not found: {old_zip}")
    if not os.path.isfile(new_zip):
        raise FileNotFoundError(f"New zip not found: {new_zip}")

    bin_ext = set(binary_extensions) if binary_extensions else DEFAULT_BINARY_EXTENSIONS
    # Normalize to lowercase with dots
    bin_ext = {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in bin_ext}

    with tempfile.TemporaryDirectory() as tmp_dir:
        old_dir = os.path.join(tmp_dir, 'old')
        new_dir = os.path.join(tmp_dir, 'new')

        # Extract both zips
        with zipfile.ZipFile(old_zip, 'r') as zf:
            zf.extractall(old_dir)
        with zipfile.ZipFile(new_zip, 'r') as zf:
            zf.extractall(new_dir)

        # Find effective roots (handle single-directory wrappers)
        old_root = _find_root_dir(old_dir)
        new_root = _find_root_dir(new_dir)

        # Build file inventories
        old_files = _walk_files(old_root, bin_ext)
        new_files = _walk_files(new_root, bin_ext)

        old_paths = set(old_files.keys())
        new_paths = set(new_files.keys())

        # Categorize
        only_old = old_paths - new_paths   # candidates for removed/moved
        only_new = new_paths - old_paths   # candidates for added/moved
        common = old_paths & new_paths

        # Compute hashes for move detection
        old_hashes = {}  # hash -> [path, ...]
        for path in only_old:
            h = _file_hash(old_files[path])
            old_hashes.setdefault(h, []).append(path)

        new_hashes = {}  # hash -> [path, ...]
        for path in only_new:
            h = _file_hash(new_files[path])
            new_hashes.setdefault(h, []).append(path)

        # Detect moves: same content, different path
        moved = []
        moved_old = set()
        moved_new = set()
        for h in old_hashes:
            if h in new_hashes:
                # Match them up (pair by position in each list)
                old_list = old_hashes[h]
                new_list = new_hashes[h]
                for i in range(min(len(old_list), len(new_list))):
                    moved.append((old_list[i], new_list[i]))
                    moved_old.add(old_list[i])
                    moved_new.add(new_list[i])

        # Final classification
        added = sorted(p for p in only_new if p not in moved_new)
        removed = sorted(p for p in only_old if p not in moved_old)
        moved.sort(key=lambda x: x[1])  # sort by new path

        # Check common files for modifications
        modified = []
        unchanged = []
        for path in sorted(common):
            old_h = _file_hash(old_files[path])
            new_h = _file_hash(new_files[path])
            if old_h == new_h:
                unchanged.append(path)
            else:
                fd = _compute_diff(old_files[path], new_files[path], path, max_diff_lines)
                if fd:
                    modified.append(fd)
                else:
                    # Files differ by hash but diff couldn't be computed (binary content?)
                    unchanged.append(path)

        # Compute total diff lines
        total_diff_lines = sum(fd.diff_line_count for fd in modified)

        # Count total lines in new snapshot
        total_lines_in_new = 0
        for path in new_files:
            total_lines_in_new += _count_lines(new_files[path])

        # Detect status documents in new snapshot
        status_docs = {}
        for path in new_files:
            if _is_status_doc(path):
                content = _read_text_file(new_files[path])
                if content is not None:
                    status_docs[path] = ''.join(content)

        # Find status doc diffs (subset of modified)
        status_doc_diffs = [fd for fd in modified if _is_status_doc(fd.path)]

        new_file_listing = sorted(new_files.keys())
        old_file_listing = sorted(old_files.keys())

        return SnapshotDiff(
            added=added,
            removed=removed,
            modified=modified,
            moved=moved,
            unchanged=unchanged,
            total_diff_lines=total_diff_lines,
            files_changed_count=len(added) + len(removed) + len(modified) + len(moved),
            new_file_listing=new_file_listing,
            old_file_listing=old_file_listing,
            total_lines_in_new=total_lines_in_new,
            status_docs=status_docs,
            status_doc_diffs=status_doc_diffs,
        )


def get_snapshot_files(zip_path: str,
                       binary_extensions: Optional[list[str]] = None
                       ) -> tuple[list[str], dict[str, str]]:
    """
    Extract a single snapshot and return its file listing and contents.

    Used for generating the initial project summary.

    Args:
        zip_path: Path to the zip file
        binary_extensions: Extensions to skip

    Returns:
        (file_listing, {relative_path: file_content_string})
    """
    if not os.path.isfile(zip_path):
        raise FileNotFoundError(f"Zip not found: {zip_path}")

    bin_ext = set(binary_extensions) if binary_extensions else DEFAULT_BINARY_EXTENSIONS
    bin_ext = {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in bin_ext}

    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(tmp_dir)

        root = _find_root_dir(tmp_dir)
        files = _walk_files(root, bin_ext)

        file_listing = sorted(files.keys())
        file_contents = {}
        for rel_path, abs_path in files.items():
            lines = _read_text_file(abs_path)
            if lines is not None:
                file_contents[rel_path] = ''.join(lines)

        return file_listing, file_contents


if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(errors='replace')
    if len(sys.argv) != 3:
        print("Usage: python snapshot_diff.py <old_zip> <new_zip>")
        sys.exit(1)

    result = diff_snapshots(sys.argv[1], sys.argv[2])
    print(f"Added:     {len(result.added)} files")
    print(f"Removed:   {len(result.removed)} files")
    print(f"Modified:  {len(result.modified)} files")
    print(f"Moved:     {len(result.moved)} files")
    print(f"Unchanged: {len(result.unchanged)} files")
    print(f"Total diff lines: {result.total_diff_lines}")
    print(f"Total lines in new snapshot: {result.total_lines_in_new}")
    if result.status_docs:
        print(f"Status docs found: {list(result.status_docs.keys())}")

    if result.added:
        print("\nAdded files:")
        for p in result.added[:10]:
            print(f"  + {p}")
    if result.removed:
        print("\nRemoved files:")
        for p in result.removed[:10]:
            print(f"  - {p}")
    if result.moved:
        print("\nMoved files:")
        for old, new in result.moved[:10]:
            print(f"  {old} -> {new}")
    if result.modified:
        print("\nModified files:")
        for fd in result.modified[:5]:
            print(f"  ~ {fd.path} ({fd.diff_line_count} diff lines)")
            # Show first few diff lines
            for line in fd.diff_text.split('\n')[:8]:
                print(f"    {line}")
            if fd.diff_line_count > 8:
                print(f"    ... ({fd.diff_line_count - 8} more lines)")
