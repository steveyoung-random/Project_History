# Project History Analyzer - Implementation Plan (v2)

## Context

You have a directory (`D:\OneDrive\My Software`) with 664+ zip files representing snapshots of various coding projects over time. You want a tool that, given a project name, walks through each consecutive pair of snapshots, diffs them, uses LLM calls to describe what changed and why, and produces a Markdown narrative of the project's evolution. The goal is to reconstruct development history: what was built, what problems were encountered, how they were solved, and what lessons were learned.

## Key Design Principles

1. **Local-first**: Compute ALL diffs locally before making any API calls. This gives us data to plan intelligently.
2. **Adaptive breakpoints**: Use statistical analysis of change magnitudes to determine what's "minor" vs "significant" for each specific project - no hardcoded thresholds.
3. **Project understanding via detailed summary**: Generate a detailed architectural summary from the initial snapshot, used as stable cached context for all subsequent LLM calls.
4. **Status document awareness**: Detect and leverage developer-written status/notes documents within snapshots for additional context about intent.
5. **Cost-efficient API usage**: Anthropic prompt caching for stable context, local API response caching for resumability, intelligent batching of minor changes.
6. **Drill-down capability**: Any two snapshots (contiguous or not) can be compared and analyzed on demand.

## File Structure

```
D:\VS Projects\Project_History\
├── api_keys.py                  # existing - API keys
├── config.json                  # NEW - project configuration (DONE)
├── CLAUDE.md                    # existing
├── IMPLEMENTATION_PLAN.md       # this file
├── STATUS.md                    # project status tracking
├── analyze_project.py           # NEW - main entry point / CLI
├── snapshot_discovery.py        # NEW - find, parse, sort zip files
├── snapshot_diff.py             # NEW - extract zips, compute diffs
├── change_analyzer.py           # NEW - local analysis: magnitudes, breakpoints, analysis planning
├── llm_analysis.py              # NEW - LLM prompts and analysis pipeline
├── report_generator.py          # NEW - Markdown report assembly
├── progress_tracker.py          # NEW - resumability tracking
├── utils/
│   ├── ai_client.py             # MODIFIED - system_message param added to QueryWithBaseClient (DONE)
│   ├── api_cache.py             # existing - reuse as-is
│   ├── config.py                # MODIFIED - generalized error messages (DONE)
│   ├── error_handling.py        # existing - reuse as-is
│   └── ...                      # other utils unchanged
└── output/                      # generated reports go here
```

## Overall Pipeline

```
Phase 1: Discovery       snapshot_discovery.py    Find and sort all zip snapshots for a project
Phase 2: Local Diffing    snapshot_diff.py         Extract and diff ALL consecutive pairs (no API calls)
Phase 3: Analysis Plan    change_analyzer.py       Statistical breakpoints, batch grouping, plan API calls
Phase 4: Understanding    llm_analysis.py          Generate detailed project summary from first snapshot
Phase 5: LLM Analysis     llm_analysis.py          Execute planned analysis units (batched or individual)
Phase 6: Report           report_generator.py      Assemble Markdown narrative with overview
```

## 1. config.json (DONE)

Already created. Contains model configs, binary extension list, zip directory path, diff thresholds.

## 2. snapshot_discovery.py - Find and Sort Snapshots

**Key function:** `discover_snapshots(zip_directory: str, project_name: str) -> list[SnapshotInfo]`

- Scans directory for zip files matching `{project_name}_*.zip` (case-insensitive on the project name portion)
- Parses the suffix to extract a sort key using these patterns:
  - `YYYYMMDD[letter]` (e.g., `20250923b`) -> datetime + letter ordinal
  - `YY-MM-DD` or `YY_MM_DD` (e.g., `22-08-01`) -> datetime
  - `MM-DD-YY` or `MM_DD_YY` (e.g., `02-27-21`) -> datetime (disambiguate: if first segment > 12, it's MM-DD-YY)
  - `M-DD-YY` (e.g., `8-14-21`) -> no-zero-pad variant
  - `NNNN` (e.g., `0235`) -> integer sequence
- Returns sorted list of `SnapshotInfo(path, sort_key, label)` dataclasses
- Raises error if < 2 snapshots found
- Fails with clear error on unparseable names (correctness over silent dropping)

**Edge cases observed in real data:**
- Case variations in project names: `Arduino_sketches` vs `Arduino_Sketches` (case-insensitive match)
- Typos: `Arduino_sketeches` (won't match `Arduino_sketches` - correct behavior)
- Underscore vs dash separators in dates: both `12-22-21` and `12_22_21` exist
- No-zero-pad months: `8-14-21` instead of `08-14-21`

## 3. snapshot_diff.py - Extract and Compare

**Key function:** `diff_snapshots(old_zip: str, new_zip: str, binary_extensions: list[str]) -> SnapshotDiff`

- Extracts both zips to temporary directories (using `tempfile.TemporaryDirectory`)
- Walks both directory trees, building file inventories (skipping binary extensions)
- Categorizes files as:
  - **Added**: in new but not old
  - **Removed**: in old but not new
  - **Modified**: in both, content differs (SHA256 hash comparison)
  - **Unchanged**: in both, content identical
- **Move detection**: For each removed+added pair, compare file content hashes. If a removed file's hash matches an added file's hash, classify as **Moved** (old_path -> new_path)
- For modified files: generate unified diff (using `difflib.unified_diff`)
- **Status document detection**: Identify files matching status document patterns (see section 4.1)
- Returns `SnapshotDiff` dataclass:
  ```python
  @dataclass
  class FileDiff:
      path: str
      diff_lines: list[str]   # unified diff lines
      diff_line_count: int

  @dataclass
  class SnapshotDiff:
      added: list[str]                    # paths of added files
      removed: list[str]                  # paths of removed files
      modified: list[FileDiff]            # modified files with diffs
      moved: list[tuple[str, str]]        # (old_path, new_path)
      unchanged: list[str]                # paths of unchanged files
      total_diff_lines: int               # sum of all diff lines
      files_changed_count: int            # added + removed + modified + moved
      new_file_listing: list[str]         # all non-binary files in new snapshot
      status_docs: dict[str, str]         # filename -> content for detected status docs in new snapshot
      status_doc_diffs: list[FileDiff]    # diffs for status docs that changed (subset of modified)
  ```

**Also needed:** `get_snapshot_files(zip_path: str, binary_extensions: list[str]) -> tuple[list[str], dict[str, str]]`
- Returns (file listing, {filename: content}) for generating project summary from a single snapshot
- Used in Phase 4 for the initial project understanding

## 4. change_analyzer.py - Local Analysis and Planning

This is the new module that does the statistical analysis to determine breakpoints and plan API calls.

### 4.1 Status Document Detection

**Function:** `detect_status_docs(file_list: list[str]) -> list[str]`

Identifies files that are likely developer status/notes documents. Match patterns:
- Exact names (case-insensitive): `STATUS.md`, `CHANGELOG.md`, `TODO.md`, `NOTES.md`, `README.md`, `DEVELOPMENT.md`, `DEVLOG.md`, `HISTORY.md`, `CLAUDE.md`, `PROGRESS.md`
- Pattern matches: `devlog*`, `changelog*`, `release_notes*`
- Content heuristic (if needed): files containing patterns like "## Current Work", "## Known Issues", "## TODO"

### 4.2 Change Magnitude Calculation

**Function:** `compute_magnitude(diff: SnapshotDiff, total_project_lines: int) -> float`

Computes a normalized change magnitude. Factors:
- `diff_ratio = total_diff_lines / total_project_lines` (how much of the project changed relative to its size)
- `structural_change = (len(added) + len(removed) + len(moved)) / total_files` (file-level restructuring)
- Combined into a single magnitude score, weighted to emphasize structural changes

### 4.3 Adaptive Breakpoint Detection

**Function:** `find_breakpoints(magnitudes: list[float]) -> BreakpointResult`

Given the magnitudes for all consecutive transitions:

1. **Compute distribution statistics**: mean, median, std dev, quartiles
2. **Find natural breaks**: Use the Jenks natural breaks algorithm (or a simpler gap-based approach) to classify magnitudes into groups. The algorithm:
   - Sort magnitudes
   - Compute gaps between consecutive sorted values
   - Large gaps indicate natural boundaries between groups
   - For small projects (few snapshots), use simpler percentile-based splits
3. **Output**: `BreakpointResult` with:
   - `minor_threshold: float` - magnitudes below this are "minor"
   - `major_threshold: float` - magnitudes above this are "major"
   - Everything in between is "moderate"
   - `distribution_stats: dict` - for reporting/debugging

### 4.4 Analysis Unit Planning

**Function:** `plan_analysis_units(snapshots: list[SnapshotInfo], diffs: list[SnapshotDiff], magnitudes: list[float], breakpoints: BreakpointResult) -> list[AnalysisUnit]`

Groups transitions into analysis units:

```python
@dataclass
class AnalysisUnit:
    snapshot_range: tuple[int, int]    # indices into snapshot list (start, end)
    transitions: list[int]             # indices into diffs list
    tier: str                          # 'minor_batch', 'moderate', 'major'
    total_magnitude: float
    description: str                   # e.g., "Snapshots 12-17 (5 minor transitions)"
```

Logic:
- Consecutive transitions all below `minor_threshold` are batched into one `minor_batch` unit
- Transitions between `minor_threshold` and `major_threshold` are individual `moderate` units
- Transitions above `major_threshold` are individual `major` units
- A `minor_batch` with only 1 transition is treated as an individual minor unit (no point batching)

## 5. llm_analysis.py - LLM Analysis Pipeline

### 5.1 Project Summary Generation

**Function:** `generate_project_summary(file_listing: list[str], file_contents: dict[str, str], status_docs: dict[str, str], project_name: str, ai_client: BaseAIClient) -> str`

Called once at the start (Phase 4). Sends key source files to the LLM and asks for a detailed architectural summary:

- **System message**: "You are a software architect analyzing a codebase to understand its structure and purpose."
- **Cached context**: The source file contents (sent as cache-eligible blocks)
- **Query**: "Provide a detailed architectural summary of this project. For each significant file/module, describe: its purpose, key classes/functions, and how it relates to other modules. Also describe the overall architecture, design patterns used, and the project's apparent purpose."
- **Status docs**: If status docs exist in the first snapshot, include them: "The developer has also included the following project documentation: [contents]"

The resulting summary (typically 1000-3000 tokens) becomes the stable cached context for all subsequent calls.

**Summary refresh**: At inflection points (after a major change), regenerate the summary using the post-change snapshot. The old summary is included in the prompt: "The project has undergone significant changes. Here is the previous architectural summary: [old summary]. Here is the current codebase: [files]. Provide an updated architectural summary reflecting the changes."

### 5.2 Change Analysis by Tier

**Function:** `analyze_changes(unit: AnalysisUnit, diffs: list[SnapshotDiff], snapshots: list[SnapshotInfo], project_summary: str, ai_client: BaseAIClient) -> AnalysisResult`

All tiers share the same prompt prefix structure for maximum cache reuse:
```
[System message - stable]
[Project summary - stable, cached]
[Status doc content if present - semi-stable, cached when unchanged]
---
[Query with diffs - varies per call]
```

**Minor batch** (batched small transitions):
- Single LLM call
- Query includes a summary of all transitions in the batch: files changed, total diffs
- Asks for: brief overview of what work was done across these versions
- Output: paragraph summary

**Moderate** (individual transition):
- Single LLM call
- Query includes full diffs for all modified files
- If status docs changed, highlight those changes prominently: "The developer's status document changed as follows: [diff]"
- Asks for: what was changed, likely motivation, any patterns
- Output: 1-2 paragraph description

**Major** (individual transition, deep analysis):
- **Call 1**: Structural analysis - send file listings (old vs new), added/removed/moved files. Ask: what was reorganized and why?
- **Call 2**: Code analysis - send diffs (chunked if >5000 lines). Ask: what specific code changes were made and what do they accomplish?
- **Call 3**: Synthesis - send results of calls 1 and 2 plus status doc changes. Ask: synthesize into a narrative covering what changed, why, what problems were being solved, and what the implications are.
- Output: multi-paragraph narrative

### 5.3 Prompt Caching Strategy

- **System message** (stable across all calls): ~100 tokens, always cached
- **Project summary** (stable between refreshes): 1000-3000 tokens, cached via `AIMessage(role="system", cache=project_summary)`
- **Status doc content** (semi-stable): included as a second cached block when present and >1024 tokens (Anthropic's minimum for cache benefit)
- Calls within the same tier-3 analysis share cached context (calls happen within seconds, well within 5-minute TTL)
- Sequential processing of analysis units means consecutive calls benefit from cache of previous call's prefix

### 5.4 Overview Generation

**Function:** `generate_overview(project_name: str, all_analyses: list[AnalysisResult], ai_client: BaseAIClient) -> str`

After all analysis units are processed:
- Concatenate all individual analysis summaries as cached context
- Ask the LLM to produce a high-level narrative arc: what was the project's evolution, what were the major phases of development, what roadblocks were encountered, what lessons were learned?

## 6. report_generator.py - Markdown Report

**Key function:** `generate_report(project_name: str, analyses: list[AnalysisResult], overview: str, output_dir: str) -> str`

Produces `output/{project_name}_history.md`:

```markdown
# Project History: {project_name}

## Overview
{LLM-generated project narrative}

## Change Statistics
- Total snapshots analyzed: N
- Analysis units: N (M minor batches, N moderate, P major)
- Date range: {first_label} to {last_label}

## Version History

### {old_label} -> {new_label}
**Files changed:** N modified, N added, N removed, N moved

{LLM analysis narrative}

<details><summary>File details</summary>

**Modified:** file1.py, file2.py
**Added:** file3.py
**Removed:** old_file.py
**Moved:** old/path.py -> new/path.py
</details>

---
### {batch label: snapshots 12-17}
**Covers:** 5 transitions with minor changes

{LLM batch analysis narrative}

<details><summary>File details per transition</summary>
...
</details>

---
### {next...}
```

## 7. progress_tracker.py - Resumability

- Stores progress in `output/{project_name}_progress.json`
- Tracks:
  ```json
  {
    "project_name": "...",
    "snapshots_hash": "...",         // hash of snapshot list to detect if zips changed
    "local_analysis_complete": true, // Phase 2-3 done?
    "project_summary": "...",        // cached project summary text
    "completed_units": [0, 1, 2],    // indices of completed analysis units
    "analysis_results": {...},       // stored results per unit
    "last_updated": "..."
  }
  ```
- If `snapshots_hash` changes (new zips added), invalidates and restarts
- Local diffing (Phase 2) results can be re-derived cheaply so don't need to be persisted
- Analysis results ARE persisted so we don't re-call the LLM
- The overview is regenerated on each complete run (one API call, likely cached anyway)

## 8. analyze_project.py - CLI Entry Point

```
python analyze_project.py <project_name> [--zip-dir PATH] [--output-dir PATH] [--model MODEL_NAME]
python analyze_project.py <project_name> --drill-down <label_A> <label_B>  # compare any two snapshots
python analyze_project.py --list-projects [--zip-dir PATH]                  # list available projects
```

- Uses `argparse`
- `--zip-dir` overrides config.json's `zip_directory`
- `--output-dir` overrides config.json's output directory
- `--model` overrides `current_engine`
- `--drill-down` compares two specific snapshots (by label) with full detailed analysis regardless of magnitude
- `--list-projects` scans the zip directory and lists all detected project names with snapshot counts

**Main flow:**
1. Load config, set up cache file, create AI client
2. Discover and sort snapshots
3. Print snapshot list summary
4. **Phase 2**: Diff all consecutive pairs locally (with progress bar)
5. **Phase 3**: Compute magnitudes, find breakpoints, plan analysis units. Print the plan.
6. **Phase 4**: Generate project summary from first snapshot (or load from progress)
7. **Phase 5**: For each analysis unit (skipping completed ones):
   a. Analyze with LLM
   b. Store result in progress tracker
   c. Print progress
8. **Phase 6**: Generate overview, assemble report
9. Print summary and report path

**Drill-down flow:**
1. Load config, set up cache file, create AI client
2. Discover snapshots, find the two requested labels
3. Diff the two snapshots
4. Load or generate project summary
5. Run full (tier-3 style) analysis regardless of magnitude
6. Print result to console and optionally append to report

## 9. Implementation Order

1. **snapshot_discovery.py** - can be tested independently with real zip directory
2. **snapshot_diff.py** - can be tested with a pair of real zips
3. **change_analyzer.py** - can be tested with diff results from step 2
4. **progress_tracker.py** - simple JSON read/write
5. **llm_analysis.py** - the core analysis pipeline
6. **report_generator.py** - Markdown assembly
7. **analyze_project.py** - wire everything together
8. **Testing** - verify against real data
9. **STATUS.md, DEVELOPER_GUIDE.md** - project documentation

## 10. Verification

- Run `snapshot_discovery.py` against the real zip directory with several project names to verify pattern parsing
- Run `snapshot_diff.py` on a pair of known zips and inspect the diff output
- Run `change_analyzer.py` on a project with many snapshots (e.g., SimpleCCompiler with 57) and inspect the breakpoint analysis and planned units
- Run `analyze_project.py` on a small project (e.g., PlaylistExporter with only 3 snapshots) end-to-end
- Verify API caching: run the same project twice, confirm second run uses cached responses
- Verify resumability: interrupt a run, restart, confirm it skips completed units
- Test drill-down mode on a specific pair
- Run on a larger project to test at scale

## What's Already Done

- `config.json` created
- `utils/config.py` modified: generalized error messages
- `utils/ai_client.py` modified: added `system_message` parameter to `QueryWithBaseClient()`
