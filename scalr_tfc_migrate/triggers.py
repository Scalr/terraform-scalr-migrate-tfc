"""VCS trigger pattern validation."""
from typing import List, Optional

from scalr_tfc_migrate.console import ConsoleOutput


def validate_trigger_pattern(pattern: str) -> bool:
    """
    Validate a trigger pattern format.
    Returns True if the pattern is valid, False otherwise.
    """
    # Skip validation for comments
    if pattern.startswith('#'):
        return True

    # Basic validation rules:
    # 1. Pattern should not be empty after stripping
    # 2. Pattern should not contain invalid characters
    # 3. Pattern should follow gitignore-like syntax

    pattern = pattern.strip()
    if not pattern:
        return False

    # Check for invalid characters (if any)
    # Note: Scalr uses gitignore-like syntax, so most characters are valid
    invalid_chars = ['\n', '\r']  # Newlines are not allowed in patterns
    if any(char in pattern for char in invalid_chars):
        return False

    return True


def handle_trigger_patterns(patterns: List[str]) -> Optional[str]:
    """
    Process and validate trigger patterns.
    Returns a multiline string of valid patterns or None if no valid patterns exist.
    """
    try:
        if not patterns:
            return None

        validated_patterns = []
        for pattern in patterns:
            if validate_trigger_pattern(pattern):
                validated_patterns.append(pattern)
            else:
                ConsoleOutput.warning(f"Invalid trigger pattern: {pattern}")

        return "\n".join(validated_patterns)
    except Exception as e:
        ConsoleOutput.error(f"Error processing trigger patterns: {str(e)}")
        return None
