"""Workspace name matching shared by the migrator and the auxiliary scripts."""
import fnmatch
import re


def matches_workspace_patterns(workspace_name: str, patterns: str) -> bool:
    """
    Match a workspace name against a comma-separated list of patterns.

    Patterns containing '*' or '?' are matched as anchored, case-insensitive shell
    globs; every other pattern is a case-insensitive exact match.
    """
    for pattern in patterns.split(','):
        # Clean the pattern by removing quotes and whitespace
        cleaned_pattern = pattern.replace("'", '').replace('"', '').strip()

        # Skip empty patterns
        if not cleaned_pattern:
            continue

        if '*' in cleaned_pattern or '?' in cleaned_pattern:
            # fnmatch.translate anchors the pattern (start-to-end), so
            # "prod-*" matches "prod-network" but not "xprod-network-yy"
            regex_pattern = fnmatch.translate(cleaned_pattern)
            if re.match(regex_pattern, workspace_name, re.IGNORECASE):
                return True
        else:
            # True exact match, case-insensitive. Previously this branch
            # used an unanchored re.search, which meant "network" would
            # also match "core-network-prod" (substring, not exact).
            if cleaned_pattern.lower() == workspace_name.lower():
                return True
    return False
