"""
Change Analyzer Module

Performs local-only analysis of snapshot diffs to:
1. Compute normalized change magnitudes
2. Find adaptive breakpoints using natural breaks in the distribution
3. Plan analysis units (batching minor changes, flagging major ones)
"""

import math
from dataclasses import dataclass, field
from snapshot_diff import SnapshotDiff


@dataclass
class BreakpointResult:
    """Result of adaptive breakpoint detection."""
    minor_threshold: float      # magnitudes below this are "minor"
    major_threshold: float      # magnitudes above this are "major"
    distribution_stats: dict    # for reporting/debugging


@dataclass
class AnalysisUnit:
    """A planned unit of LLM analysis."""
    snapshot_range: tuple[int, int]   # indices into snapshot list (start, end inclusive)
    transitions: list[int]            # indices into diffs list
    tier: str                         # 'minor_batch', 'minor', 'moderate', 'major'
    total_magnitude: float
    description: str                  # e.g., "Snapshots 12-17 (5 minor transitions)"
    is_inflection_point: bool = False # True if project summary should be refreshed after this


def compute_magnitude(diff: SnapshotDiff) -> float:
    """
    Compute a normalized change magnitude for a snapshot transition.

    The magnitude reflects how much the project changed, normalized against
    project size so that small and large projects are comparable.

    Returns a float >= 0. Typical values:
        0.0 - 0.01: trivial (whitespace, comments)
        0.01 - 0.05: minor (small bug fixes)
        0.05 - 0.20: moderate (feature work)
        0.20+: major (restructuring, rewrites)
    """
    total_lines = max(diff.total_lines_in_new, 1)  # avoid division by zero
    total_files = max(len(diff.new_file_listing), 1)

    # Diff ratio: how much of the codebase changed (by line count)
    diff_ratio = diff.total_diff_lines / total_lines

    # Structural change: file-level additions/removals/moves relative to total files
    structural_changes = len(diff.added) + len(diff.removed) + len(diff.moved)
    structural_ratio = structural_changes / total_files

    # Modification breadth: what fraction of files were modified
    modification_breadth = len(diff.modified) / total_files

    # Combined magnitude with weights
    # Structural changes are weighted higher because they indicate reorganization
    magnitude = (
        0.4 * diff_ratio +
        0.35 * structural_ratio +
        0.25 * modification_breadth
    )

    return magnitude


def find_breakpoints(magnitudes: list[float]) -> BreakpointResult:
    """
    Find adaptive breakpoints in the distribution of change magnitudes.

    Uses a gap-based natural breaks approach:
    1. Sort magnitudes
    2. Find the largest gaps between consecutive values
    3. Use those gaps as boundaries between minor/moderate/major

    For projects with very few transitions or uniform distributions,
    falls back to percentile-based splits.

    Args:
        magnitudes: List of magnitude values for all transitions

    Returns:
        BreakpointResult with thresholds and distribution statistics
    """
    if not magnitudes:
        return BreakpointResult(
            minor_threshold=0.05,
            major_threshold=0.20,
            distribution_stats={'method': 'default', 'count': 0}
        )

    n = len(magnitudes)
    sorted_mags = sorted(magnitudes)

    # Compute distribution statistics
    mean_val = sum(sorted_mags) / n
    median_val = sorted_mags[n // 2] if n % 2 == 1 else (sorted_mags[n // 2 - 1] + sorted_mags[n // 2]) / 2
    variance = sum((x - mean_val) ** 2 for x in sorted_mags) / n
    std_dev = math.sqrt(variance)
    q1 = sorted_mags[n // 4] if n >= 4 else sorted_mags[0]
    q3 = sorted_mags[3 * n // 4] if n >= 4 else sorted_mags[-1]

    stats = {
        'count': n,
        'min': sorted_mags[0],
        'max': sorted_mags[-1],
        'mean': round(mean_val, 4),
        'median': round(median_val, 4),
        'std_dev': round(std_dev, 4),
        'q1': round(q1, 4),
        'q3': round(q3, 4),
    }

    # For very few transitions, use simple percentile-based approach
    if n < 5:
        stats['method'] = 'percentile (few transitions)'
        return BreakpointResult(
            minor_threshold=round(median_val, 6),
            major_threshold=round(q3, 6) if n >= 4 else round(sorted_mags[-1] * 0.8, 6),
            distribution_stats=stats
        )

    # For uniform distributions (low relative spread), use percentile approach
    if std_dev < mean_val * 0.3 and mean_val > 0:
        stats['method'] = 'percentile (uniform distribution)'
        return BreakpointResult(
            minor_threshold=round(q1, 6),
            major_threshold=round(q3, 6),
            distribution_stats=stats
        )

    # Gap-based natural breaks
    # Compute gaps between consecutive sorted values
    gaps = []
    for i in range(len(sorted_mags) - 1):
        gap = sorted_mags[i + 1] - sorted_mags[i]
        gaps.append((gap, i))

    # Sort gaps by size, largest first
    gaps.sort(reverse=True)

    # We want to find 2 breakpoints (creating 3 groups)
    # Take the 2 largest gaps
    if len(gaps) >= 2:
        # Get the two largest gaps
        break_indices = sorted([gaps[0][1], gaps[1][1]])

        minor_threshold = (sorted_mags[break_indices[0]] + sorted_mags[break_indices[0] + 1]) / 2
        major_threshold = (sorted_mags[break_indices[1]] + sorted_mags[break_indices[1] + 1]) / 2

        # Ensure minor < major
        if minor_threshold >= major_threshold:
            # Only one real gap - use it for minor, set major higher
            big_gap_idx = gaps[0][1]
            minor_threshold = (sorted_mags[big_gap_idx] + sorted_mags[big_gap_idx + 1]) / 2
            major_threshold = minor_threshold + (sorted_mags[-1] - minor_threshold) * 0.5

        stats['method'] = 'gap-based natural breaks'
        stats['gap_1'] = round(gaps[0][0], 4)
        stats['gap_2'] = round(gaps[1][0], 4) if len(gaps) >= 2 else None
    else:
        # Only 2 values - use the midpoint
        minor_threshold = (sorted_mags[0] + sorted_mags[-1]) / 3
        major_threshold = 2 * (sorted_mags[0] + sorted_mags[-1]) / 3
        stats['method'] = 'midpoint (2 values)'

    return BreakpointResult(
        minor_threshold=round(minor_threshold, 6),
        major_threshold=round(major_threshold, 6),
        distribution_stats=stats
    )


def plan_analysis_units(
    snapshot_count: int,
    diffs: list[SnapshotDiff],
    magnitudes: list[float],
    breakpoints: BreakpointResult
) -> list[AnalysisUnit]:
    """
    Group transitions into analysis units based on breakpoints.

    Logic:
    - Consecutive minor transitions are batched into one unit
    - Moderate transitions are individual units
    - Major transitions are individual units with deep analysis
    - Major transitions are flagged as inflection points (project summary refresh)

    Args:
        snapshot_count: Total number of snapshots
        diffs: List of SnapshotDiff objects (one per consecutive pair)
        magnitudes: List of magnitude values (parallel to diffs)
        breakpoints: Thresholds from find_breakpoints()

    Returns:
        Ordered list of AnalysisUnit objects
    """
    if len(diffs) != len(magnitudes):
        raise ValueError(f"diffs ({len(diffs)}) and magnitudes ({len(magnitudes)}) must have same length")

    units = []
    minor_batch_start = None  # index of first transition in current minor batch
    minor_batch_transitions = []
    minor_batch_magnitude = 0.0

    def flush_minor_batch():
        """Flush accumulated minor transitions into an analysis unit."""
        nonlocal minor_batch_start, minor_batch_transitions, minor_batch_magnitude
        if not minor_batch_transitions:
            return

        if len(minor_batch_transitions) == 1:
            # Single minor transition - no point batching
            idx = minor_batch_transitions[0]
            units.append(AnalysisUnit(
                snapshot_range=(idx, idx + 1),
                transitions=[idx],
                tier='minor',
                total_magnitude=magnitudes[idx],
                description=f"Snapshot {idx} -> {idx + 1} (minor change)"
            ))
        else:
            first = minor_batch_transitions[0]
            last = minor_batch_transitions[-1]
            units.append(AnalysisUnit(
                snapshot_range=(first, last + 1),
                transitions=list(minor_batch_transitions),
                tier='minor_batch',
                total_magnitude=minor_batch_magnitude,
                description=f"Snapshots {first} -> {last + 1} ({len(minor_batch_transitions)} minor transitions)"
            ))

        minor_batch_start = None
        minor_batch_transitions = []
        minor_batch_magnitude = 0.0

    for i, mag in enumerate(magnitudes):
        if mag <= breakpoints.minor_threshold:
            # Minor transition - accumulate into batch
            if minor_batch_start is None:
                minor_batch_start = i
            minor_batch_transitions.append(i)
            minor_batch_magnitude += mag
        else:
            # Non-minor transition - flush any pending batch first
            flush_minor_batch()

            if mag >= breakpoints.major_threshold:
                tier = 'major'
                desc = f"Snapshot {i} -> {i + 1} (MAJOR change, magnitude {mag:.4f})"
                is_inflection = True
            else:
                tier = 'moderate'
                desc = f"Snapshot {i} -> {i + 1} (moderate change, magnitude {mag:.4f})"
                is_inflection = False

            units.append(AnalysisUnit(
                snapshot_range=(i, i + 1),
                transitions=[i],
                tier=tier,
                total_magnitude=mag,
                description=desc,
                is_inflection_point=is_inflection
            ))

    # Flush any remaining minor batch
    flush_minor_batch()

    return units


def summarize_plan(units: list[AnalysisUnit], magnitudes: list[float],
                   breakpoints: BreakpointResult) -> str:
    """
    Generate a human-readable summary of the analysis plan.
    """
    lines = []
    lines.append("Analysis Plan Summary")
    lines.append("=" * 50)

    # Distribution stats
    stats = breakpoints.distribution_stats
    lines.append(f"\nChange Distribution ({stats['count']} transitions):")
    lines.append(f"  Method: {stats['method']}")
    if stats['count'] > 0:
        lines.append(f"  Range:  {stats['min']:.4f} - {stats['max']:.4f}")
        lines.append(f"  Mean:   {stats['mean']:.4f}  Median: {stats['median']:.4f}")
        lines.append(f"  StdDev: {stats['std_dev']:.4f}")
    lines.append(f"\nThresholds:")
    lines.append(f"  Minor:  <= {breakpoints.minor_threshold:.4f}")
    lines.append(f"  Major:  >= {breakpoints.major_threshold:.4f}")

    # Unit counts by tier
    tier_counts = {}
    for u in units:
        tier_counts[u.tier] = tier_counts.get(u.tier, 0) + 1

    lines.append(f"\nAnalysis Units: {len(units)} total")
    for tier, count in sorted(tier_counts.items()):
        lines.append(f"  {tier}: {count}")

    inflection_count = sum(1 for u in units if u.is_inflection_point)
    if inflection_count:
        lines.append(f"  Inflection points (summary refresh): {inflection_count}")

    # Estimated API calls
    api_calls = 0
    for u in units:
        if u.tier == 'major':
            api_calls += 3  # structural + code + synthesis
        else:
            api_calls += 1  # single call
    api_calls += 1  # initial project summary
    api_calls += 1  # final overview
    lines.append(f"\nEstimated API calls: {api_calls}")
    lines.append(f"  (+ {inflection_count} summary refreshes at inflection points)")

    lines.append(f"\nPlanned Units:")
    for i, u in enumerate(units):
        marker = " ***" if u.is_inflection_point else ""
        lines.append(f"  {i + 1}. {u.description}{marker}")

    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    from snapshot_discovery import discover_snapshots
    from snapshot_diff import diff_snapshots

    if len(sys.argv) < 3:
        print("Usage: python change_analyzer.py <zip_directory> <project_name> [max_pairs]")
        sys.exit(1)

    zip_dir = sys.argv[1]
    project_name = sys.argv[2]
    max_pairs = int(sys.argv[3]) if len(sys.argv) > 3 else None

    snapshots = discover_snapshots(zip_dir, project_name)
    print(f"Found {len(snapshots)} snapshots for '{project_name}'")

    # Compute all diffs
    pairs = list(zip(snapshots[:-1], snapshots[1:]))
    if max_pairs:
        pairs = pairs[:max_pairs]

    print(f"Computing {len(pairs)} diffs...")
    all_diffs = []
    all_magnitudes = []
    for i, (old, new) in enumerate(pairs):
        print(f"  [{i + 1}/{len(pairs)}] {old.label} -> {new.label}...", end='', flush=True)
        d = diff_snapshots(old.path, new.path)
        mag = compute_magnitude(d)
        all_diffs.append(d)
        all_magnitudes.append(mag)
        print(f" magnitude={mag:.4f} ({d.files_changed_count} files, {d.total_diff_lines} diff lines)")

    # Find breakpoints and plan
    bp = find_breakpoints(all_magnitudes)
    units = plan_analysis_units(len(snapshots), all_diffs, all_magnitudes, bp)

    print()
    print(summarize_plan(units, all_magnitudes, bp))
