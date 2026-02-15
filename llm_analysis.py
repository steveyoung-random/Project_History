"""
LLM Analysis Module

Uses LLM API calls to:
1. Generate detailed project summaries from snapshot contents
2. Analyze changes between snapshots at different depth tiers
3. Generate overall project narrative overview

Uses the existing ai_client infrastructure for API calls, caching, and retry logic.
"""

import sys
import os

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dataclasses import dataclass
from typing import Optional
from snapshot_diff import SnapshotDiff, FileDiff
from change_analyzer import AnalysisUnit
from utils.ai_client import QueryWithBaseClient, BaseAIClient, make_api_call_with_retry
from tool_assisted_analysis import (
    run_tool_conversation, SnapshotContext, OverviewContext,
    SNAPSHOT_TOOLS, OVERVIEW_TOOLS,
)


SYSTEM_MESSAGE = (
    "You are an expert software engineer analyzing the evolution of a coding project. "
    "You examine code changes between snapshots to understand what was built, modified, "
    "and why. You identify patterns like bug fixes, feature additions, refactoring, "
    "architecture changes, and problem-solving approaches."
)

# Writing style instructions, included as cached context in every API call.
WRITING_STYLE = """Writing style requirements for all output:

Language and Attribution
- Keep tone neutral and factual; avoid promotional language ("revolutionary," "groundbreaking," "rich cultural heritage," "captivates").
- Don't inflate significance without evidence ("testament to," "plays a vital role," "underscores importance").
- Don't use valorizing adjectives to characterize developer decisions or judgment ("disciplined," "sophisticated," "elegant," "mature," "pragmatic"). Describe what was done, not how impressive it was.
- Avoid dramatic contrastive setups that minimize one thing to elevate another ("This wasn't merely X — it was Y," "more than just X"). State what happened directly.
- Attribute opinions and disputed facts to specific, verifiable sources rather than vague authorities ("many experts," "it is widely believed").
- Avoid editorializing or injecting unsupported analysis ("it's important to note," "defining feature").

Sentence Structure and Flow
- Vary sentence length and structure to avoid uniform rhythm.
- Minimize transitional connectors ("moreover," "furthermore," "however," "on the other hand").
- Avoid repetitive patterns like the rule of three or negative parallelisms ("not only...but").
- Don't end sections with unnecessary summaries ("In conclusion," "Overall").
- Eliminate superficial commentary that ends sentences with "-ing" phrases.

Voice and Perspective
- Never address the reader directly ("let's explore," "we will examine") unless the genre requires it.
- Avoid collaborative language ("Would you like me to...?").
- Don't include self-referential cues ("as noted above," "in this article").
- Never include knowledge cutoffs or disclaimers about limited information.

Formatting and Style
- Use sentence case for headings unless convention requires title case.
- Apply formatting (bold, italics) sparingly and purposefully.
- Avoid emojis, excessive punctuation, or decorative elements.
- Avoid em-dashes.
- Write in paragraphs rather than over-relying on bullet points.

Content Quality
- Prioritize concrete, sourced information over vague generalizations.
- Avoid padding with empty phrases or superficial depth.
- Don't overuse clichéd framings around "humanity," "innovation," or "transformative power."
- Don't assume commercial intent, product goals, or user bases. Avoid "prototype to product" framing, "productization," or language implying the goal is shipping a product. Describe the project's actual state and evolution without imposing a narrative of professional maturation.

The key principle: Write naturally, concisely, and directly, focusing on factual content rather than artificial emphasis or formulaic structures."""

MAX_DIFF_LINES_PER_FILE = 300
MAX_TOTAL_DIFF_FOR_PROMPT = 5000

# Single log file per run: set once at startup, reused for all LLM calls.
_run_logfile = None


def set_run_logfile(path):
    """Set the logfile path for this run. Call once at startup."""
    global _run_logfile
    _run_logfile = path


def get_run_logfile():
    """Get the run logfile, lazily creating one if not set."""
    global _run_logfile
    if _run_logfile is None:
        from utils.ai_client import GetLogfile
        _run_logfile = GetLogfile()
    return _run_logfile


@dataclass
class AnalysisResult:
    """Result of analyzing one analysis unit."""
    unit_index: int
    tier: str
    narrative: str                  # the LLM-generated analysis text
    snapshot_labels: list[str]      # labels of snapshots covered
    files_summary: dict             # {added: [...], removed: [...], modified: [...], moved: [...]}

    def to_dict(self) -> dict:
        return {
            'unit_index': self.unit_index,
            'tier': self.tier,
            'narrative': self.narrative,
            'snapshot_labels': self.snapshot_labels,
            'files_summary': self.files_summary,
        }

    @staticmethod
    def from_dict(d: dict) -> 'AnalysisResult':
        return AnalysisResult(
            unit_index=d['unit_index'],
            tier=d['tier'],
            narrative=d['narrative'],
            snapshot_labels=d['snapshot_labels'],
            files_summary=d['files_summary'],
        )


def _build_files_summary(diff: SnapshotDiff) -> dict:
    """Build a summary dict of file changes from a SnapshotDiff."""
    return {
        'added': diff.added,
        'removed': diff.removed,
        'modified': [fd.path for fd in diff.modified],
        'moved': [{'from': old, 'to': new} for old, new in diff.moved],
    }


def _merge_files_summaries(summaries: list[dict]) -> dict:
    """Merge multiple file summaries for batch analysis."""
    merged = {'added': [], 'removed': [], 'modified': [], 'moved': []}
    seen_added = set()
    seen_removed = set()
    seen_modified = set()
    for s in summaries:
        for f in s.get('added', []):
            if f not in seen_added:
                merged['added'].append(f)
                seen_added.add(f)
        for f in s.get('removed', []):
            if f not in seen_removed:
                merged['removed'].append(f)
                seen_removed.add(f)
        for f in s.get('modified', []):
            if f not in seen_modified:
                merged['modified'].append(f)
                seen_modified.add(f)
        merged['moved'].extend(s.get('moved', []))
    return merged


def _truncate_diff(diff_text: str, max_lines: int = MAX_DIFF_LINES_PER_FILE) -> str:
    """Truncate a diff to a maximum number of lines."""
    lines = diff_text.split('\n')
    if len(lines) <= max_lines:
        return diff_text
    return '\n'.join(lines[:max_lines]) + f"\n... ({len(lines) - max_lines} more lines truncated)"


def _format_diff_for_prompt(diff: SnapshotDiff, max_total_lines: int = MAX_TOTAL_DIFF_FOR_PROMPT) -> str:
    """Format a SnapshotDiff into text suitable for an LLM prompt."""
    sections = []

    if diff.added:
        sections.append("FILES ADDED:\n" + '\n'.join(f"  + {p}" for p in diff.added))

    if diff.removed:
        sections.append("FILES REMOVED:\n" + '\n'.join(f"  - {p}" for p in diff.removed))

    if diff.moved:
        sections.append("FILES MOVED:\n" + '\n'.join(
            f"  {old} -> {new}" for old, new in diff.moved))

    if diff.modified:
        mod_section = f"FILES MODIFIED ({len(diff.modified)} files):\n"
        total_lines_so_far = 0
        for fd in diff.modified:
            truncated = _truncate_diff(fd.diff_text)
            lines_in_this = len(truncated.split('\n'))

            if total_lines_so_far + lines_in_this > max_total_lines:
                remaining = len(diff.modified) - diff.modified.index(fd)
                mod_section += f"\n  ... and {remaining} more modified files (diffs omitted for length)\n"
                break

            mod_section += f"\n--- {fd.path} ({fd.diff_line_count} lines changed) ---\n"
            mod_section += truncated + "\n"
            total_lines_so_far += lines_in_this

        sections.append(mod_section)

    # Add status doc changes prominently if present
    if diff.status_doc_diffs:
        status_section = "DEVELOPER STATUS DOCUMENT CHANGES:\n"
        status_section += "(These documents contain the developer's own notes about what they're working on)\n"
        for fd in diff.status_doc_diffs:
            status_section += f"\n--- {fd.path} ---\n"
            status_section += _truncate_diff(fd.diff_text, 200) + "\n"
        sections.insert(0, status_section)  # Put at front for prominence

    return '\n\n'.join(sections)


def _format_batch_summary(diffs: list[SnapshotDiff], labels: list[tuple[str, str]]) -> str:
    """Format a summary of multiple transitions for batch analysis."""
    sections = []
    for i, (diff, (old_label, new_label)) in enumerate(zip(diffs, labels)):
        section = f"Transition {i + 1}: {old_label} -> {new_label}\n"
        section += f"  Files: {diff.files_changed_count} changed "
        section += f"({len(diff.added)} added, {len(diff.removed)} removed, "
        section += f"{len(diff.modified)} modified, {len(diff.moved)} moved)\n"
        section += f"  Diff lines: {diff.total_diff_lines}\n"
        if diff.modified:
            section += "  Modified: " + ', '.join(fd.path for fd in diff.modified[:10])
            if len(diff.modified) > 10:
                section += f" ... and {len(diff.modified) - 10} more"
            section += "\n"
        if diff.added:
            section += "  Added: " + ', '.join(diff.added[:10])
            if len(diff.added) > 10:
                section += f" ... and {len(diff.added) - 10} more"
            section += "\n"
        if diff.removed:
            section += "  Removed: " + ', '.join(diff.removed[:10])
            if len(diff.removed) > 10:
                section += f" ... and {len(diff.removed) - 10} more"
            section += "\n"
        sections.append(section)
    return '\n'.join(sections)


def _query_llm(ai_client: BaseAIClient, cache_parts: list[str],
               query: str, max_tokens: int = 4000) -> str:
    """
    Make an LLM query with caching and retry logic.

    The WRITING_STYLE instructions are prepended as the first cached block
    so they are shared across all calls and benefit from platform caching.

    Args:
        ai_client: The AI client to use
        cache_parts: List of strings for cached prompt context
        query: The variable query portion
        max_tokens: Max response tokens

    Returns:
        The LLM's text response
    """
    # Prepend writing style as the first cached block (stable across all calls)
    full_cache_parts = [WRITING_STYLE] + cache_parts

    def _call():
        return QueryWithBaseClient(
            ai_client=ai_client,
            cache_prompt_list=full_cache_parts,
            query_prompt=query,
            logfile=get_run_logfile(),
            json_output=False,
            max_tokens=max_tokens,
            system_message=SYSTEM_MESSAGE
        )

    result = make_api_call_with_retry(_call)
    return result if result else ""


def generate_project_summary(
    file_listing: list[str],
    file_contents: dict[str, str],
    status_docs: dict[str, str],
    project_name: str,
    ai_client: BaseAIClient
) -> str:
    """
    Generate a detailed architectural summary of the project.

    This summary is used as stable cached context for all subsequent
    change analysis calls.

    Args:
        file_listing: List of all file paths in the project
        file_contents: {path: content} for all text files
        status_docs: {path: content} for detected status documents
        project_name: Name of the project
        ai_client: AI client for making queries

    Returns:
        Detailed project summary string
    """
    # Build the source code context
    # Include all files, but truncate very large ones
    source_parts = []
    total_chars = 0
    max_chars = 100000  # ~25K tokens of source code context

    for path in sorted(file_contents.keys()):
        content = file_contents[path]
        if total_chars + len(content) > max_chars:
            # Truncate or skip remaining files
            remaining = len(file_contents) - len(source_parts)
            source_parts.append(f"\n... ({remaining} more files not shown for length)")
            break
        source_parts.append(f"\n=== {path} ===\n{content}")
        total_chars += len(content)

    source_context = ''.join(source_parts)

    # Build cache parts (source code is stable and cacheable)
    cache_parts = [
        f"Project: {project_name}\n\nFile listing ({len(file_listing)} files):\n"
        + '\n'.join(f"  {f}" for f in file_listing)
        + "\n\nSource code:\n" + source_context
    ]

    # Add status docs if present
    if status_docs:
        status_text = "\n\nDeveloper documentation found in the project:\n"
        for path, content in status_docs.items():
            status_text += f"\n--- {path} ---\n{content}\n"
        cache_parts.append(status_text)

    query = (
        "Provide a detailed architectural summary of this project. Include:\n"
        "1. The project's purpose and what it does\n"
        "2. The programming language(s) and key technologies/frameworks used\n"
        "3. For each significant file or module: its purpose, key classes/functions, "
        "and how it relates to other modules\n"
        "4. The overall architecture and design patterns used\n"
        "5. Any notable implementation details or patterns\n\n"
        "Be thorough but concise. This summary will be used as context when analyzing "
        "future code changes to this project."
    )

    print("  Generating project summary...", flush=True)
    summary = _query_llm(ai_client, cache_parts, query, max_tokens=4000)
    return summary


def analyze_minor_batch(
    unit: AnalysisUnit,
    diffs: list[SnapshotDiff],
    snapshot_labels: list[str],
    project_summary: str,
    project_name: str,
    ai_client: BaseAIClient
) -> AnalysisResult:
    """Analyze a batch of minor transitions with a single LLM call."""
    labels = []
    batch_diffs = []
    for idx in unit.transitions:
        batch_diffs.append(diffs[idx])
        labels.append((snapshot_labels[idx], snapshot_labels[idx + 1]))

    batch_summary = _format_batch_summary(batch_diffs, labels)
    all_summaries = [_build_files_summary(d) for d in batch_diffs]
    merged_summary = _merge_files_summaries(all_summaries)

    cache_parts = [
        f"Project: {project_name}\n\nProject Summary:\n{project_summary}"
    ]

    query = (
        f"The following {len(unit.transitions)} consecutive transitions represent "
        f"a period of minor changes in the project. Provide a brief overview of what "
        f"work was done across these versions.\n\n{batch_summary}"
    )

    print(f"  Analyzing batch of {len(unit.transitions)} minor transitions...", flush=True)
    narrative = _query_llm(ai_client, cache_parts, query, max_tokens=2000)

    return AnalysisResult(
        unit_index=unit.transitions[0],
        tier=unit.tier,
        narrative=narrative,
        snapshot_labels=[snapshot_labels[unit.snapshot_range[0]],
                        snapshot_labels[unit.snapshot_range[1]]],
        files_summary=merged_summary,
    )


def analyze_minor_single(
    unit: AnalysisUnit,
    diff: SnapshotDiff,
    old_label: str,
    new_label: str,
    project_summary: str,
    project_name: str,
    ai_client: BaseAIClient
) -> AnalysisResult:
    """Analyze a single minor transition."""
    diff_text = _format_diff_for_prompt(diff)

    cache_parts = [
        f"Project: {project_name}\n\nProject Summary:\n{project_summary}"
    ]

    query = (
        f"Here are the changes between version {old_label} and {new_label}. "
        f"Briefly summarize what was changed and why.\n\n{diff_text}"
    )

    print(f"  Analyzing minor change {old_label} -> {new_label}...", flush=True)
    narrative = _query_llm(ai_client, cache_parts, query, max_tokens=1500)

    return AnalysisResult(
        unit_index=unit.transitions[0],
        tier=unit.tier,
        narrative=narrative,
        snapshot_labels=[old_label, new_label],
        files_summary=_build_files_summary(diff),
    )


def analyze_moderate(
    unit: AnalysisUnit,
    diff: SnapshotDiff,
    old_label: str,
    new_label: str,
    project_summary: str,
    project_name: str,
    ai_client: BaseAIClient
) -> AnalysisResult:
    """Analyze a moderate transition with full diffs."""
    diff_text = _format_diff_for_prompt(diff)

    cache_parts = [
        f"Project: {project_name}\n\nProject Summary:\n{project_summary}"
    ]

    query = (
        f"Analyze the changes between version {old_label} and {new_label} of the project.\n\n"
        f"Changes summary: {diff.files_changed_count} files changed "
        f"({len(diff.added)} added, {len(diff.removed)} removed, "
        f"{len(diff.modified)} modified, {len(diff.moved)} moved), "
        f"{diff.total_diff_lines} diff lines.\n\n"
        f"{diff_text}\n\n"
        f"Describe:\n"
        f"1. What was changed\n"
        f"2. The likely motivation for these changes\n"
        f"3. Any patterns you observe (bug fixes, new features, refactoring, etc.)\n"
        f"4. If status documents changed, note what the developer said about their work"
    )

    print(f"  Analyzing moderate change {old_label} -> {new_label}...", flush=True)
    narrative = _query_llm(ai_client, cache_parts, query, max_tokens=3000)

    return AnalysisResult(
        unit_index=unit.transitions[0],
        tier=unit.tier,
        narrative=narrative,
        snapshot_labels=[old_label, new_label],
        files_summary=_build_files_summary(diff),
    )


def analyze_major(
    unit: AnalysisUnit,
    diff: SnapshotDiff,
    old_label: str,
    new_label: str,
    project_summary: str,
    project_name: str,
    ai_client: BaseAIClient,
    old_zip_path: str = '',
    new_zip_path: str = '',
    binary_extensions: list[str] | None = None,
) -> AnalysisResult:
    """
    Deep analysis of a major transition using tool-assisted conversation.

    The LLM receives a high-level change summary and can pull diffs,
    file contents, and listings on demand via tools. No truncation is
    applied -- the LLM decides what to read and how much.
    """
    ctx = SnapshotContext(diff, old_zip_path, new_zip_path, binary_extensions)

    cached_context = [
        WRITING_STYLE,
        f"Project: {project_name}\n\nProject Summary:\n{project_summary}",
    ]

    # Give the LLM a compact starting point with stats
    summary = ctx.get_change_summary()
    initial_query = (
        f"MAJOR TRANSITION: {old_label} -> {new_label}\n\n"
        f"Change statistics:\n"
        f"  Files added:     {summary['files_added']}\n"
        f"  Files removed:   {summary['files_removed']}\n"
        f"  Files modified:  {summary['files_modified']}\n"
        f"  Files moved:     {summary['files_moved']}\n"
        f"  Total diff lines: {summary['total_diff_lines']}\n"
        f"  Total lines in new snapshot: {summary['total_lines_in_new_snapshot']}\n\n"
        "You have tools to explore this transition in detail. Use them to:\n"
        "1. List the modified/added/removed files to understand the scope\n"
        "2. Read diffs for files that seem significant\n"
        "3. Read file contents when a diff needs more context\n"
        "4. Check status docs for the developer's own notes\n\n"
        "After investigating, write a comprehensive narrative covering:\n"
        "- What changed at a high level\n"
        "- Why these changes were likely made\n"
        "- What problems were being solved\n"
        "- The impact on the project's architecture\n"
        "- Any lessons that can be inferred from the changes\n\n"
        "Write in a clear, narrative style suitable for a project history document."
    )

    print(f"  Analyzing major change {old_label} -> {new_label} (tool-assisted)...", flush=True)
    narrative = run_tool_conversation(
        ai_client=ai_client,
        system_message=SYSTEM_MESSAGE,
        cached_context=cached_context,
        initial_query=initial_query,
        tools=SNAPSHOT_TOOLS,
        tool_handlers=ctx.get_tool_handlers(),
        max_turns=25,
        max_tokens=4000,
    )

    return AnalysisResult(
        unit_index=unit.transitions[0],
        tier=unit.tier,
        narrative=narrative,
        snapshot_labels=[old_label, new_label],
        files_summary=_build_files_summary(diff),
    )


def refresh_project_summary(
    old_summary: str,
    file_listing: list[str],
    file_contents: dict[str, str],
    status_docs: dict[str, str],
    project_name: str,
    ai_client: BaseAIClient
) -> str:
    """
    Refresh the project summary after a major change (inflection point).
    """
    source_parts = []
    total_chars = 0
    max_chars = 100000

    for path in sorted(file_contents.keys()):
        content = file_contents[path]
        if total_chars + len(content) > max_chars:
            remaining = len(file_contents) - len(source_parts)
            source_parts.append(f"\n... ({remaining} more files not shown for length)")
            break
        source_parts.append(f"\n=== {path} ===\n{content}")
        total_chars += len(content)

    source_context = ''.join(source_parts)

    cache_parts = [
        f"Project: {project_name}\n\nPrevious architectural summary:\n{old_summary}\n\n"
        f"Current source code:\n{source_context}"
    ]

    if status_docs:
        status_text = "\n\nCurrent developer documentation:\n"
        for path, content in status_docs.items():
            status_text += f"\n--- {path} ---\n{content}\n"
        cache_parts.append(status_text)

    query = (
        "The project has undergone significant changes since the previous summary. "
        "Provide an updated architectural summary reflecting the current state. "
        "Note what has changed from the previous architecture."
    )

    print("  Refreshing project summary after major change...", flush=True)
    return _query_llm(ai_client, cache_parts, query, max_tokens=4000)


def generate_overview(
    project_name: str,
    all_results: list[AnalysisResult],
    ai_client: BaseAIClient,
    snapshot_labels: list[str] | None = None,
) -> str:
    """
    Generate a high-level overview narrative of the entire project's evolution.

    For small numbers of transitions (<= 10), uses a single one-shot call.
    For larger projects, uses tool-assisted conversation so the LLM can
    pull individual transition summaries on demand without hitting context limits.
    """
    # For small projects, one-shot is simpler and sufficient
    if len(all_results) <= 10:
        return _generate_overview_oneshot(project_name, all_results, ai_client)

    return _generate_overview_tool_assisted(
        project_name, all_results, ai_client, snapshot_labels or []
    )


def _generate_overview_oneshot(
    project_name: str,
    all_results: list[AnalysisResult],
    ai_client: BaseAIClient,
) -> str:
    """One-shot overview for small projects (original approach)."""
    analyses_text = ""
    for r in all_results:
        label_range = f"{r.snapshot_labels[0]} -> {r.snapshot_labels[-1]}"
        analyses_text += f"\n### {label_range} ({r.tier})\n{r.narrative}\n"

    cache_parts = [
        f"Project: {project_name}\n\n"
        f"Individual analysis results for {len(all_results)} transitions:\n"
        + analyses_text
    ]

    query = (
        "Based on all the individual transition analyses above, write a high-level "
        "narrative overview of this project's evolution. Cover:\n"
        "1. What the project is and its overall purpose\n"
        "2. The major phases of development\n"
        "3. Key milestones and turning points\n"
        "4. Significant challenges or roadblocks encountered and how they were addressed\n"
        "5. Architectural evolution and design decisions\n"
        "6. Lessons that can be inferred from the development history\n\n"
        "Write in a clear, engaging narrative style. This is the executive summary "
        "that readers will see first."
    )

    print("  Generating project overview...", flush=True)
    return _query_llm(ai_client, cache_parts, query, max_tokens=4000)


def _generate_overview_tool_assisted(
    project_name: str,
    all_results: list[AnalysisResult],
    ai_client: BaseAIClient,
    snapshot_labels: list[str],
) -> str:
    """Tool-assisted overview for large projects."""
    ctx = OverviewContext(all_results, snapshot_labels)

    cached_context = [WRITING_STYLE]

    # Build a compact transition index for the LLM
    transition_index = f"Project: {project_name}\n\n"
    transition_index += f"Total transitions: {len(all_results)}\n\n"
    transition_index += "Transition index:\n"
    for i, r in enumerate(all_results):
        label_range = f"{r.snapshot_labels[0]} -> {r.snapshot_labels[-1]}"
        transition_index += f"  [{i}] {label_range} (tier: {r.tier})\n"

    initial_query = (
        f"{transition_index}\n"
        "You have tools to read individual transition narratives by index or range.\n"
        "Use them to build a high-level narrative overview of this project's evolution.\n\n"
        "Approach:\n"
        "1. Read the major/moderate transitions first for key milestones\n"
        "2. Sample minor transitions for context on incremental work\n"
        "3. Write a cohesive narrative covering:\n"
        "   - What the project is and its overall purpose\n"
        "   - The major phases of development\n"
        "   - Key milestones and turning points\n"
        "   - Significant challenges or roadblocks encountered and how they were addressed\n"
        "   - Architectural evolution and design decisions\n"
        "   - Lessons that can be inferred from the development history\n\n"
        "Write in a clear, engaging narrative style. This is the executive summary "
        "that readers will see first."
    )

    print(f"  Generating project overview (tool-assisted, {len(all_results)} transitions)...", flush=True)
    return run_tool_conversation(
        ai_client=ai_client,
        system_message=SYSTEM_MESSAGE,
        cached_context=cached_context,
        initial_query=initial_query,
        tools=OVERVIEW_TOOLS,
        tool_handlers=ctx.get_tool_handlers(),
        max_turns=25,
        max_tokens=4000,
    )


def analyze_unit(
    unit: AnalysisUnit,
    diffs: list[SnapshotDiff],
    snapshot_labels: list[str],
    project_summary: str,
    project_name: str,
    ai_client: BaseAIClient,
    snapshot_paths: list[str] | None = None,
    binary_extensions: list[str] | None = None,
) -> AnalysisResult:
    """
    Dispatch to the appropriate analysis function based on unit tier.

    This is the main entry point for analyzing a single analysis unit.

    Args:
        snapshot_paths: List of zip file paths for all snapshots (needed for
            major tier tool-assisted analysis). If None, major analysis falls
            back to tool-assisted mode without file content access.
        binary_extensions: Extensions to skip when reading file contents.
    """
    if unit.tier == 'minor_batch':
        return analyze_minor_batch(
            unit, diffs, snapshot_labels, project_summary, project_name, ai_client
        )

    # All other tiers use a single transition
    idx = unit.transitions[0]
    diff = diffs[idx]
    old_label = snapshot_labels[idx]
    new_label = snapshot_labels[idx + 1]

    if unit.tier == 'minor':
        return analyze_minor_single(
            unit, diff, old_label, new_label,
            project_summary, project_name, ai_client
        )
    elif unit.tier == 'moderate':
        return analyze_moderate(
            unit, diff, old_label, new_label,
            project_summary, project_name, ai_client
        )
    elif unit.tier == 'major':
        old_zip = snapshot_paths[idx] if snapshot_paths else ''
        new_zip = snapshot_paths[idx + 1] if snapshot_paths else ''
        return analyze_major(
            unit, diff, old_label, new_label,
            project_summary, project_name, ai_client,
            old_zip_path=old_zip,
            new_zip_path=new_zip,
            binary_extensions=binary_extensions,
        )
    else:
        raise ValueError(f"Unknown tier: {unit.tier}")
