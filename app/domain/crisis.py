"""Crisis detection: fast regex-based keyword matching for safety intervention."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CrisisDetectionResult:
    """Result when crisis keywords are detected in user text."""

    severity: str  # "high" | "medium"
    matched_pattern: str
    aid_message: dict[str, Any]


class CrisisDetector:
    """Fast regex-based crisis keyword detector.

    Loads compiled patterns from YAML config for <100ms detection.
    """

    def __init__(self, config_path: str = "config/crisis_keywords.yaml"):
        self._config_path = config_path
        self._patterns: list[tuple[re.Pattern[str], str]] = []
        self._aid_message: dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load and compile crisis keyword patterns from YAML."""
        path = Path(self._config_path)
        if not path.exists():
            raise FileNotFoundError(f"Crisis keywords config not found: {self._config_path}")

        with open(path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        # Compile regex patterns for fast matching
        for entry in config.get("keywords", []):
            pattern = re.compile(entry["pattern"])
            severity = entry["severity"]
            self._patterns.append((pattern, severity))

        # Store aid message
        self._aid_message = config.get("aid_message", {})

    def detect(self, text: str) -> CrisisDetectionResult | None:
        """
        Check text against crisis keywords.

        Returns None if no match, or CrisisDetectionResult with severity and aid_message.
        Synchronous — pure CPU, fast regex matching (<100ms).
        """
        if not text:
            return None

        for pattern, severity in self._patterns:
            match = pattern.search(text)
            if match:
                return CrisisDetectionResult(
                    severity=severity,
                    matched_pattern=match.group(),
                    aid_message=self._aid_message,
                )

        return None

    def get_aid_message(self) -> dict[str, Any]:
        """Return the configured aid message structure."""
        return self._aid_message
