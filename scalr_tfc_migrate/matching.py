"""Workspace name matching shared by the migrator and the auxiliary scripts."""
import fnmatch
import os
import re
from typing import Set

from scalr_tfc_migrate.errors import InvalidInputError


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


def load_workspace_names(path: str) -> Set[str]:
    """
    Read a wave file: one exact workspace name per line, `#` comments and blank lines ignored.
    Names are returned lower-cased, matching the case-insensitive exact match of --workspaces.
    """
    if not os.path.exists(path):
        raise InvalidInputError(f"Workspaces file '{path}' does not exist")

    names: Set[str] = set()
    with open(path, 'r') as f:
        for line in f:
            name = line.split('#', 1)[0].strip()
            if name:
                names.add(name.lower())

    if not names:
        raise InvalidInputError(f"Workspaces file '{path}' does not list any workspace")
    return names
