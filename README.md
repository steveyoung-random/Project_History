# Project History Analyzer

This project will reconstruct the development history of a coding project from zip file snapshots. I know that there are better ways to store and track your coding projects over time (this is being hosted on Github, after all), but I still tend to do as I have done for years and use batch files to zip up my project directory and add either an incremental number or a date to it, and store it in a directory that is backed up to the cloud.  This makes it a bit of a challenge to go back to look at the state of code at some earlier time, but at least it's possible.  Also, I have literally decades of project snapshots stored this way, and I realized I would like a way to dig into those old projects to reconstruct their history.

This code diffs consecutive snapshots locally, then uses statistical analysis to classify changes by magnitude.  It then calls into an LLM API to generate narrative descriptions of what changed and why. Finally, it produces a report in markdown format (I use Obsidian for viewing them).

## How it works

The pipeline has six phases:

1. **Discovery** - Finds and sorts zip snapshots by date/version label.
2. **Local diffing** - Extracts and diffs all consecutive pairs (no API calls).
3. **Planning** - Computes change magnitudes and uses gap-based natural breaks to classify transitions into minor/moderate/major tiers.
4. **Project understanding** - Sends the first snapshot's source code to the LLM for an architectural summary.
5. **Analysis** - Analyzes each transition at a depth matching its tier. Minor changes get brief summaries. Major transitions use tool-assisted conversations where the LLM can pull specific diffs and file contents on demand.
6. **Report generation** - Assembles everything into a markdown narrative.

Progress is saved after each step, so interrupted runs resume where they left off.

## Example output

[SimpleCCompiler history report](examples/simpleccompiler_history.md) - generated from 57 snapshots of a simple C compiler project.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.

## Requirements

- Python 3.10+
- `anthropic` and/or `openai` package
- An API key for at least one provider

## Setup

1. Clone the repo
2. Install dependencies: `pip install anthropic openai`
3. Create `api_keys.py` with your keys:
   ```python
   anthropic_key = "sk-ant-..."
   openai_key = "sk-..."
   ```
4. Edit `config.json` to set `zip_directory` to the folder containing your zip snapshots

Snapshots should be zip files named like `ProjectName_YYYYMMDD.zip`, `ProjectName_v2.zip`, `ProjectName_0003.zip`, etc. The tool auto-detects several date and version naming patterns.

## Usage

```bash
# List available projects (auto-detected from zip filenames)
python analyze_project.py --list-projects

# Analyze a project
python analyze_project.py <project_name>

# Preview the analysis plan without making API calls
python analyze_project.py <project_name> --plan-only

# Compare two specific snapshots
python analyze_project.py <project_name> --drill-down <label_A> <label_B>

# Override defaults
python analyze_project.py <project_name> --zip-dir PATH --output-dir PATH --model MODEL_NAME
```

## Output

- `output/<project_name>_history.md` - The generated report
- `output/<project_name>_progress.json` - Resume state
- `output/api_cache.json` - Cached API responses (avoids duplicate calls on re-runs)

## Configuration

`config.json` controls model selection, file paths, and binary extension filtering. CLI flags override `zip_directory`, `output.directory`, and `current_engine`.

## Project structure

```
analyze_project.py          CLI entry point, orchestrates the pipeline
snapshot_discovery.py       Finds and sorts zip snapshots by project name
snapshot_diff.py            Extracts zips, diffs file trees, detects moves
change_analyzer.py          Magnitude calculation, breakpoint detection, tier planning
llm_analysis.py             LLM prompting for summaries and change analysis
tool_assisted_analysis.py   Multi-turn tool-calling for major transitions
progress_tracker.py         JSON-based resumability
report_generator.py         Markdown report assembly
utils/ai_client.py          Anthropic and OpenAI client abstraction
utils/api_cache.py          Local response cache
utils/config.py             Configuration loading
```