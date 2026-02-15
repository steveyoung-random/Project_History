"""
Project History Analyzer - Main Entry Point

Analyzes zip file snapshots of a coding project to reconstruct its
development history using LLM-powered change analysis.

Usage:
    python analyze_project.py <project_name> [options]
    python analyze_project.py --list-projects [--zip-dir PATH]
    python analyze_project.py <project_name> --drill-down <label_A> <label_B>
"""

import sys
import os
import argparse
import json

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from snapshot_discovery import discover_snapshots, list_projects
from snapshot_diff import diff_snapshots, get_snapshot_files, _is_status_doc
from change_analyzer import (
    compute_magnitude, find_breakpoints, plan_analysis_units, summarize_plan
)
from progress_tracker import ProgressTracker
from llm_analysis import (
    generate_project_summary, analyze_unit, generate_overview,
    refresh_project_summary, analyze_major, AnalysisResult,
    set_run_logfile
)
from report_generator import generate_report
from utils.config import get_config
from utils.ai_client import create_ai_client, GetLogfile
from utils.api_cache import set_cache_file


def load_config_with_overrides(args):
    """Load config.json and apply command-line overrides."""
    config = get_config()

    if args.zip_dir:
        config['zip_directory'] = args.zip_dir
    if args.output_dir:
        config['output'] = config.get('output', {})
        config['output']['directory'] = args.output_dir
    if hasattr(args, 'model') and args.model:
        config['current_engine'] = args.model

    return config


def get_output_dir(config):
    """Get and create the output directory."""
    output_dir = config.get('output', {}).get('directory', './output')
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def cmd_list_projects(args):
    """List all projects found in the zip directory."""
    config = load_config_with_overrides(args)
    zip_dir = config.get('zip_directory', '')

    if not zip_dir:
        print("Error: No zip_directory configured. Use --zip-dir or set in config.json.", file=sys.stderr)
        sys.exit(1)

    projects = list_projects(zip_dir)
    if not projects:
        print(f"No projects with 2+ snapshots found in: {zip_dir}")
        return

    print(f"Projects in {zip_dir}:")
    print(f"{'Project Name':<35} {'Snapshots':>10}")
    print("-" * 47)
    for name, count in projects.items():
        print(f"  {name:<33} {count:>8}")
    print(f"\n{len(projects)} projects found.")


def cmd_drill_down(args):
    """Compare two specific snapshots with full analysis."""
    config = load_config_with_overrides(args)
    zip_dir = config.get('zip_directory', '')
    output_dir = get_output_dir(config)
    binary_ext = config.get('binary_extensions', [])

    # Set up cache and logging
    cache_file = os.path.join(output_dir, 'api_cache.json')
    set_cache_file(cache_file)
    set_run_logfile(GetLogfile(output_dir))

    # Create AI client
    ai_client = create_ai_client(config=config)

    # Discover snapshots
    snapshots = discover_snapshots(zip_dir, args.project_name)

    # Find the two requested snapshots
    snap_a = None
    snap_b = None
    for s in snapshots:
        if s.label == args.drill_down[0]:
            snap_a = s
        if s.label == args.drill_down[1]:
            snap_b = s

    if snap_a is None:
        print(f"Error: Snapshot '{args.drill_down[0]}' not found.", file=sys.stderr)
        print(f"Available labels: {', '.join(s.label for s in snapshots)}", file=sys.stderr)
        sys.exit(1)
    if snap_b is None:
        print(f"Error: Snapshot '{args.drill_down[1]}' not found.", file=sys.stderr)
        print(f"Available labels: {', '.join(s.label for s in snapshots)}", file=sys.stderr)
        sys.exit(1)

    # Ensure correct order
    if snap_a.sort_key > snap_b.sort_key:
        snap_a, snap_b = snap_b, snap_a

    print(f"Drill-down analysis: {snap_a.label} -> {snap_b.label}")

    # Load or generate project summary
    tracker = ProgressTracker(args.project_name, output_dir)
    project_summary = tracker.get_project_summary()

    if not project_summary:
        print("\nPhase 1: Generating project understanding...")
        file_listing, file_contents = get_snapshot_files(snap_a.path, binary_ext)
        # Check for status docs
        from change_analyzer import detect_status_docs
        status_doc_names = detect_status_docs(file_listing) if hasattr(sys.modules.get('change_analyzer', None), 'detect_status_docs') else []
        status_docs = {name: file_contents[name] for name in status_doc_names if name in file_contents}
        project_summary = generate_project_summary(
            file_listing, file_contents, status_docs, args.project_name, ai_client
        )
        tracker.set_project_summary(project_summary)

    print("\nPhase 2: Diffing snapshots...")
    diff = diff_snapshots(snap_a.path, snap_b.path, binary_ext)
    print(f"  {diff.files_changed_count} files changed, {diff.total_diff_lines} diff lines")

    print("\nPhase 3: Deep analysis...")
    # Create a synthetic major analysis unit
    from change_analyzer import AnalysisUnit
    unit = AnalysisUnit(
        snapshot_range=(0, 1),
        transitions=[0],
        tier='major',
        total_magnitude=compute_magnitude(diff),
        description=f"Drill-down: {snap_a.label} -> {snap_b.label}",
        is_inflection_point=False
    )

    result = analyze_major(
        unit, diff, snap_a.label, snap_b.label,
        project_summary, args.project_name, ai_client,
        old_zip_path=snap_a.path, new_zip_path=snap_b.path,
        binary_extensions=binary_ext,
    )

    print("\n" + "=" * 60)
    print(f"ANALYSIS: {snap_a.label} -> {snap_b.label}")
    print("=" * 60)
    print(result.narrative)
    print("=" * 60)


def cmd_analyze(args):
    """Main analysis pipeline."""
    config = load_config_with_overrides(args)
    zip_dir = config.get('zip_directory', '')
    output_dir = get_output_dir(config)
    binary_ext = config.get('binary_extensions', [])

    if not zip_dir:
        print("Error: No zip_directory configured. Use --zip-dir or set in config.json.", file=sys.stderr)
        sys.exit(1)

    # Set up API cache and AI client (skip if plan-only)
    ai_client = None
    if not args.plan_only:
        cache_file = os.path.join(output_dir, 'api_cache.json')
        set_cache_file(cache_file)
        set_run_logfile(GetLogfile(output_dir))
        ai_client = create_ai_client(config=config)
        model_name = getattr(ai_client, 'model', 'unknown')
        print(f"Using model: {model_name}")

    # Phase 1: Discovery
    print(f"\nPhase 1: Discovering snapshots for '{args.project_name}'...")
    try:
        snapshots = discover_snapshots(zip_dir, args.project_name)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"  Found {len(snapshots)} snapshots")
    print(f"  Range: {snapshots[0].label} to {snapshots[-1].label}")

    snapshot_labels = [s.label for s in snapshots]

    # Check progress
    tracker = ProgressTracker(args.project_name, output_dir)
    snapshots_hash = tracker.compute_snapshots_hash([s.path for s in snapshots])

    if not tracker.is_valid_for(snapshots_hash):
        print("  Starting fresh analysis (snapshot set changed or no prior progress)")
        tracker.initialize(snapshots_hash, len(snapshots))
    else:
        completed = tracker.get_completed_count()
        print(f"  Resuming: {completed} units previously completed")

    # Phase 2: Local diffing
    print(f"\nPhase 2: Computing {len(snapshots) - 1} diffs locally...")
    all_diffs = []
    all_magnitudes = []
    for i in range(len(snapshots) - 1):
        old_snap = snapshots[i]
        new_snap = snapshots[i + 1]
        print(f"  [{i + 1}/{len(snapshots) - 1}] {old_snap.label} -> {new_snap.label}...",
              end='', flush=True)
        d = diff_snapshots(old_snap.path, new_snap.path, binary_ext)
        mag = compute_magnitude(d)
        all_diffs.append(d)
        all_magnitudes.append(mag)
        print(f" {d.files_changed_count} files, {d.total_diff_lines} lines, mag={mag:.4f}")

    # Phase 3: Analysis planning
    print(f"\nPhase 3: Planning analysis...")
    breakpoints = find_breakpoints(all_magnitudes)
    units = plan_analysis_units(len(snapshots), all_diffs, all_magnitudes, breakpoints)
    print(summarize_plan(units, all_magnitudes, breakpoints))

    # Stop here if --plan-only
    if args.plan_only:
        print("\n--plan-only: Stopping before API calls.")
        print("To proceed with full analysis, run again without --plan-only.")
        return

    # Phase 4: Project understanding
    print(f"\nPhase 4: Project understanding...")
    project_summary = tracker.get_project_summary()
    if project_summary:
        print("  Using cached project summary")
    else:
        print("  Generating project summary from first snapshot...")
        file_listing, file_contents = get_snapshot_files(snapshots[0].path, binary_ext)
        status_docs = {path: file_contents[path]
                       for path in file_listing
                       if _is_status_doc(path) and path in file_contents}

        project_summary = generate_project_summary(
            file_listing, file_contents, status_docs, args.project_name, ai_client
        )
        tracker.set_project_summary(project_summary)
        print(f"  Summary generated ({len(project_summary)} chars)")

    # Phase 5: LLM analysis
    print(f"\nPhase 5: Analyzing {len(units)} units...")
    all_results = []
    for i, unit in enumerate(units):
        # Check if already completed
        if tracker.is_unit_completed(i):
            stored = tracker.get_unit_result(i)
            if stored:
                all_results.append(AnalysisResult.from_dict(stored))
                print(f"  [{i + 1}/{len(units)}] {unit.description} - CACHED")
                continue

        print(f"  [{i + 1}/{len(units)}] {unit.description}")
        snapshot_paths = [s.path for s in snapshots]
        result = analyze_unit(
            unit, all_diffs, snapshot_labels, project_summary, args.project_name, ai_client,
            snapshot_paths=snapshot_paths, binary_extensions=binary_ext,
        )
        all_results.append(result)
        tracker.mark_unit_completed(i, result.to_dict())

        # Refresh project summary at inflection points
        if unit.is_inflection_point:
            # Get files from the post-change snapshot
            post_idx = unit.snapshot_range[1]
            if post_idx < len(snapshots):
                post_listing, post_contents = get_snapshot_files(
                    snapshots[post_idx].path, binary_ext
                )
                post_status = {path: post_contents[path]
                              for path in post_listing
                              if _is_status_doc(path) and path in post_contents}
                project_summary = refresh_project_summary(
                    project_summary, post_listing, post_contents,
                    post_status, args.project_name, ai_client
                )
                tracker.set_project_summary(project_summary)
                print(f"  Project summary refreshed ({len(project_summary)} chars)")

    # Phase 6: Report generation
    print(f"\nPhase 6: Generating report...")
    overview = generate_overview(args.project_name, all_results, ai_client, snapshot_labels)

    report_path = generate_report(
        args.project_name, overview, all_results, units,
        snapshot_labels, breakpoints, output_dir
    )
    print(f"\nReport written to: {report_path}")
    print(f"Analysis complete: {len(all_results)} units analyzed across {len(snapshots)} snapshots.")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze the evolution of a coding project through zip snapshots."
    )

    # Global options
    parser.add_argument('--zip-dir', type=str, default=None,
                       help='Directory containing zip files (overrides config.json)')
    parser.add_argument('--output-dir', type=str, default=None,
                       help='Output directory for reports (overrides config.json)')

    # Subcommands via mutually exclusive options
    parser.add_argument('project_name', nargs='?', default=None,
                       help='Project name to analyze (e.g., Document_Analyzer)')
    parser.add_argument('--list-projects', action='store_true',
                       help='List all projects found in the zip directory')
    parser.add_argument('--model', type=str, default=None,
                       help='Model to use (overrides config.json current_engine)')
    parser.add_argument('--drill-down', nargs=2, metavar=('LABEL_A', 'LABEL_B'),
                       help='Compare two specific snapshots with deep analysis')
    parser.add_argument('--plan-only', action='store_true',
                       help='Run local analysis only (phases 1-3): discover, diff, and plan. No API calls.')

    args = parser.parse_args()

    if args.list_projects:
        cmd_list_projects(args)
    elif args.project_name and args.drill_down:
        cmd_drill_down(args)
    elif args.project_name:
        cmd_analyze(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
