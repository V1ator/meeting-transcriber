"""Shared runtime paths for pipeline modules and tests."""

from pathlib import Path


BASE = Path(__file__).parent
RECORDINGS = BASE / "recordings"
TRANSCRIPTS = BASE / "transcripts"
NOTES = BASE / "notes"
FAILED = BASE / "failed"
