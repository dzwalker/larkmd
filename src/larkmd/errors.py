"""Exception hierarchy for larkmd."""

from __future__ import annotations


class LarkmdError(Exception):
    """Base class for all larkmd errors."""


class ConfigError(LarkmdError):
    """Raised when larkmd.yaml is invalid or required ${VAR} env vars are missing."""


class LarkCliError(LarkmdError):
    """Raised when a `lark-cli` invocation returns non-zero or empty stdout.

    Carries the original stderr so callers can pattern-match Feishu API codes
    (e.g. 1770041 schema mismatch, 131005 wiki delete scope, status 3 importer).
    """

    def __init__(self, message: str, *, returncode: int, stderr: str, cmd: list[str]):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = cmd


class SchemaMismatchError(LarkCliError):
    """Feishu API code 1770041 — payload schema rejected by the descendant API."""


class ImporterStuckError(LarkmdError):
    """`move_docs_to_wiki` poll exhausted retries without a `success` status."""


class StateIncompatibleError(LarkmdError):
    """State file's tenant or wiki_space_id differs from current config — abort to avoid wiping the wrong workspace."""
