# Developer Guide

## Architecture

The system follows a 6-phase pipeline, each handled by a dedicated module:

```
Phase 1: Discovery       snapshot_discovery.py    Find and sort zip snapshots
Phase 2: Local Diffing    snapshot_diff.py         Extract and diff ALL pairs locally
Phase 3: Analysis Plan    change_analyzer.py       Statistical breakpoints, batch grouping
Phase 4: Understanding    llm_analysis.py          Generate project summary from first snapshot
Phase 5: LLM Analysis     llm_analysis.py          Analyze each unit (batched or individual)
Phase 6: Report           report_generator.py      Assemble Markdown narrative
```

Orchestrated by `analyze_project.py` (CLI entry point) with `progress_tracker.py` enabling resumability.

## Key Design Decisions

### Local-First
All diffs are computed locally (Phase 2) before any API calls. This allows statistical analysis of the change distribution to plan API usage intelligently.

### Adaptive Breakpoints
Change magnitudes are normalized against project size and classified using gap-based natural breaks detection. No hardcoded thresholds - each project gets thresholds that fit its own distribution.

### Three Analysis Tiers
- **Minor** (below minor_threshold): Quick summary, single API call. Consecutive minor transitions are batched.
- **Moderate** (between thresholds): Full diffs sent, single API call for detailed analysis.
- **Major** (above major_threshold): 3-call deep analysis (structural + code + synthesis). Triggers project summary refresh.

### Prompt Caching Strategy
- System message + project summary are sent as cached context (stable prefix)
- Anthropic's ephemeral cache gives 90% discount on cached tokens
- Sequential processing means consecutive calls benefit from each other's cache
- Local API response cache (api_cache.py) prevents duplicate API calls on re-runs

### Status Document Awareness
The system detects files like STATUS.md, CHANGELOG.md, TODO.md within snapshots. Changes to these documents are highlighted prominently in analysis, as they contain developer-written context about intent.

## Module Reference

### snapshot_discovery.py
- `discover_snapshots(zip_dir, project_name)` -> sorted list of SnapshotInfo
- `list_projects(zip_dir)` -> dict of project names to snapshot counts
- Handles: YYYYMMDD, YYMMDD, YY-MM-DD, MM-DD-YY, M-D-YY, NNNN, N.N, vN patterns

### snapshot_diff.py
- `diff_snapshots(old_zip, new_zip, binary_extensions)` -> SnapshotDiff
- `get_snapshot_files(zip_path, binary_extensions)` -> (file_listing, file_contents)
- Detects single-directory wrappers in zips, handles encoding fallback

### change_analyzer.py
- `compute_magnitude(diff)` -> float (normalized 0.0-1.0 scale)
- `find_breakpoints(magnitudes)` -> BreakpointResult
- `plan_analysis_units(...)` -> list of AnalysisUnit

### llm_analysis.py
- `generate_project_summary(...)` -> str
- `analyze_unit(unit, diffs, labels, summary, name, client)` -> AnalysisResult
- `generate_overview(name, results, client)` -> str
- `refresh_project_summary(...)` -> str

### progress_tracker.py
- `ProgressTracker(project_name, output_dir)` - JSON-based state tracking
- Invalidates when snapshot set changes (hash-based detection)

### report_generator.py
- `generate_report(...)` -> path to generated .md file

## Dependencies
- Python 3.10+ (for dataclass, type hints)
- `anthropic` package (for Claude API)
- `openai` package (for OpenAI/Azure API)
- All other imports are stdlib

## Configuration (config.json)
- `models` - Model configurations (platform, model ID, max_tokens)
- `current_engine` - Default model name
- `zip_directory` - Where to find zip files
- `binary_extensions` - File extensions to skip
- `output.directory` - Where to write reports
- CLI flags can override zip_directory, output directory, and model
