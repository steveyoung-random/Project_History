"""
Progress Tracker Module

Provides resumability for the analysis pipeline by tracking:
- Which analysis units have been completed
- Cached project summary
- Analysis results for completed units

Progress is stored in a JSON file per project in the output directory.
If the snapshot list changes (new zips added/removed), progress is invalidated.
"""

import json
import os
import hashlib
import tempfile
from datetime import datetime
from typing import Optional, Any


class ProgressTracker:
    """
    Tracks analysis progress for a single project.

    Stores state in output/{project_name}_progress.json.
    """

    def __init__(self, project_name: str, output_dir: str):
        self.project_name = project_name
        self.output_dir = output_dir
        self.progress_file = os.path.join(output_dir, f"{project_name}_progress.json")
        self._data: dict = {}
        self._load()

    def _load(self):
        """Load progress from disk if it exists."""
        if os.path.isfile(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: Could not load progress file, starting fresh: {e}")
                self._data = {}
        else:
            self._data = {}

    def _save(self):
        """Save progress to disk atomically."""
        os.makedirs(self.output_dir, exist_ok=True)
        self._data['last_updated'] = datetime.now().isoformat()

        # Atomic write: write to temp file then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=self.output_dir, suffix='.tmp', prefix='progress_'
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2)
            # On Windows, need to remove target first
            if os.path.exists(self.progress_file):
                os.remove(self.progress_file)
            os.rename(tmp_path, self.progress_file)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def compute_snapshots_hash(snapshot_paths: list[str]) -> str:
        """Compute a hash of the snapshot list to detect changes."""
        content = '\n'.join(sorted(snapshot_paths))
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def is_valid_for(self, snapshots_hash: str) -> bool:
        """Check if the saved progress is still valid for the current snapshot set."""
        return self._data.get('snapshots_hash') == snapshots_hash

    def initialize(self, snapshots_hash: str, snapshot_count: int):
        """Initialize or reset progress for a new analysis run."""
        self._data = {
            'project_name': self.project_name,
            'snapshots_hash': snapshots_hash,
            'snapshot_count': snapshot_count,
            'project_summary': None,
            'completed_units': [],
            'analysis_results': {},
            'last_updated': datetime.now().isoformat()
        }
        self._save()

    def get_project_summary(self) -> Optional[str]:
        """Get the cached project summary, if any."""
        return self._data.get('project_summary')

    def set_project_summary(self, summary: str):
        """Store the project summary."""
        self._data['project_summary'] = summary
        self._save()

    def is_unit_completed(self, unit_index: int) -> bool:
        """Check if a specific analysis unit has been completed."""
        return unit_index in self._data.get('completed_units', [])

    def mark_unit_completed(self, unit_index: int, result: dict[str, Any]):
        """
        Mark an analysis unit as completed and store its result.

        Args:
            unit_index: Index of the completed unit
            result: Analysis result dict (must be JSON-serializable)
        """
        completed = self._data.setdefault('completed_units', [])
        if unit_index not in completed:
            completed.append(unit_index)
            completed.sort()
        self._data.setdefault('analysis_results', {})[str(unit_index)] = result
        self._save()

    def get_unit_result(self, unit_index: int) -> Optional[dict]:
        """Get the stored result for a completed analysis unit."""
        return self._data.get('analysis_results', {}).get(str(unit_index))

    def get_all_results(self) -> dict[int, dict]:
        """Get all stored analysis results, keyed by unit index."""
        raw = self._data.get('analysis_results', {})
        return {int(k): v for k, v in raw.items()}

    def get_completed_count(self) -> int:
        """Get the number of completed analysis units."""
        return len(self._data.get('completed_units', []))

    def get_status_summary(self, total_units: int) -> str:
        """Get a human-readable status summary."""
        completed = self.get_completed_count()
        has_summary = self.get_project_summary() is not None
        return (
            f"Progress: {completed}/{total_units} units completed, "
            f"project summary {'cached' if has_summary else 'not yet generated'}"
        )
