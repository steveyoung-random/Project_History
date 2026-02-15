# Project History Analyzer - Status

## Current State: Fully Implemented and Tested

## What This Project Is
A Python tool that analyzes zip file snapshots of coding projects to reconstruct development history. Given a project name, it diffs consecutive snapshots locally, uses statistical analysis to determine breakpoints, generates a detailed project understanding via LLM, then analyzes changes with LLM calls at appropriate granularity. Produces a Markdown narrative report.

## Completed

### All modules implemented and working:
- **config.json** - Project configuration
- **snapshot_discovery.py** - Find and sort zip files by project name (handles YYYYMMDD, YYMMDD, YY-MM-DD, MM-DD-YY, NNNN, version, vN patterns)
- **snapshot_diff.py** - Extract zips to temp dirs, diff file trees, detect moves, compute change stats
- **change_analyzer.py** - Statistical breakpoint detection, magnitude calculation, analysis unit planning
- **progress_tracker.py** - JSON-based resumability tracking
- **llm_analysis.py** - Project summary generation + tiered change analysis (minor/moderate/major)
- **report_generator.py** - Markdown report assembly
- **analyze_project.py** - CLI entry point with main flow, list-projects, and drill-down modes
- **utils/ai_client.py** - Modified: added `system_message` parameter to `QueryWithBaseClient()`
- **utils/config.py** - Modified: generalized error messages

### Tested:
- End-to-end with PlaylistExporter (3 snapshots) - produced detailed, high-quality report
- Resumability verified: second run uses cached results, no duplicate API calls
- Snapshot discovery tested with multiple projects and naming patterns
- Diff engine tested with various project pairs
- Change analyzer tested with Mentorship_Database (18 snapshots) - adaptive breakpoints work well

## How to Use

```bash
# List all available projects
python analyze_project.py --list-projects

# Analyze a project
python analyze_project.py <project_name>

# Drill-down on specific snapshots
python analyze_project.py <project_name> --drill-down <label_A> <label_B>

# Override settings
python analyze_project.py <project_name> --zip-dir PATH --output-dir PATH --model MODEL_NAME
```

## Output
Reports are written to `./output/{project_name}_history.md`.
Progress files at `./output/{project_name}_progress.json` enable resumability.
API response cache at `./output/api_cache.json`.
