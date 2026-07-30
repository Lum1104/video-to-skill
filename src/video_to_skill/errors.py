"""Domain errors with stable exit semantics."""


class VideoToSkillError(Exception):
    """Base class for expected, user-actionable failures."""


class ConfigurationError(VideoToSkillError):
    """Configuration is missing or invalid."""


class DependencyError(VideoToSkillError):
    """A required external program or optional backend is unavailable."""


class SourceError(VideoToSkillError):
    """An input source cannot be inspected or materialized."""


class ProcessingError(VideoToSkillError):
    """A media-processing stage failed."""


class ValidationError(VideoToSkillError):
    """A generated skill violates the package contract."""
