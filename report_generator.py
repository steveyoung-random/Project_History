"""
Report Generator Module

Assembles Markdown reports from LLM analysis results.
Produces a chronological narrative of project evolution.
"""

import os
from datetime import datetime
from llm_analysis import AnalysisResult
from change_analyzer import AnalysisUnit, BreakpointResult


def generate_report(
    project_name: str,
    overview: str,
    analysis_results: list[AnalysisResult],
    units: list[AnalysisUnit],
    snapshot_labels: list[str],
    breakpoints: BreakpointResult,
    output_dir: str
) -> str:
    """
    Generate the final Markdown report.

    Args:
        project_name: Name of the project
        overview: LLM-generated project overview narrative
        analysis_results: Ordered list of analysis results
        units: Analysis units for stats
        snapshot_labels: All snapshot labels for reference
        breakpoints: Breakpoint info for stats
        output_dir: Where to write the report

    Returns:
        Path to the generated report file
    """
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, f"{project_name}_history.md")

    # Count tiers
    tier_counts = {}
    for u in units:
        tier_counts[u.tier] = tier_counts.get(u.tier, 0) + 1

    lines = []

    # Header
    lines.append(f"# Project History: {project_name}")
    lines.append("")
    lines.append(f"*Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")

    # Overview
    lines.append("## Overview")
    lines.append("")
    lines.append(overview)
    lines.append("")

    # Statistics
    lines.append("## Change Statistics")
    lines.append("")
    lines.append(f"- **Total snapshots:** {len(snapshot_labels)}")
    lines.append(f"- **Analysis units:** {len(units)}")
    for tier in ['major', 'moderate', 'minor', 'minor_batch']:
        if tier in tier_counts:
            label = tier.replace('_', ' ')
            lines.append(f"  - {label}: {tier_counts[tier]}")
    lines.append(f"- **Date range:** {snapshot_labels[0]} to {snapshot_labels[-1]}")
    stats = breakpoints.distribution_stats
    lines.append(f"- **Breakpoint method:** {stats.get('method', 'N/A')}")
    lines.append(f"- **Thresholds:** minor <= {breakpoints.minor_threshold:.4f}, "
                 f"major >= {breakpoints.major_threshold:.4f}")
    lines.append("")

    # Version History
    lines.append("## Version History")
    lines.append("")

    for result in analysis_results:
        label_range = f"{result.snapshot_labels[0]} -> {result.snapshot_labels[-1]}"

        # Section header with tier indicator
        tier_marker = ""
        if result.tier == 'major':
            tier_marker = " (Major Change)"
        elif result.tier == 'minor_batch':
            tier_marker = " (Minor Changes)"

        lines.append(f"### {label_range}{tier_marker}")
        lines.append("")

        # File change summary
        fs = result.files_summary
        parts = []
        if fs.get('modified'):
            parts.append(f"{len(fs['modified'])} modified")
        if fs.get('added'):
            parts.append(f"{len(fs['added'])} added")
        if fs.get('removed'):
            parts.append(f"{len(fs['removed'])} removed")
        if fs.get('moved'):
            parts.append(f"{len(fs['moved'])} moved")

        if parts:
            lines.append(f"**Files changed:** {', '.join(parts)}")
            lines.append("")

        # Narrative
        lines.append(result.narrative)
        lines.append("")

        # Collapsible file details
        if any(fs.get(k) for k in ('modified', 'added', 'removed', 'moved')):
            lines.append("<details><summary>File details</summary>")
            lines.append("")
            if fs.get('modified'):
                lines.append("**Modified:**")
                for f in fs['modified']:
                    lines.append(f"- {f}")
                lines.append("")
            if fs.get('added'):
                lines.append("**Added:**")
                for f in fs['added']:
                    lines.append(f"- {f}")
                lines.append("")
            if fs.get('removed'):
                lines.append("**Removed:**")
                for f in fs['removed']:
                    lines.append(f"- {f}")
                lines.append("")
            if fs.get('moved'):
                lines.append("**Moved:**")
                for m in fs['moved']:
                    lines.append(f"- {m['from']} -> {m['to']}")
                lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")

    # Write report
    content = '\n'.join(lines)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return report_path
