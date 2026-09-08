class RepositoryIndexError(RuntimeError):
    pass


class IndexRunNotFoundError(RepositoryIndexError):
    pass


class IndexNotFoundError(RepositoryIndexError):
    pass


class IndexWorkspaceError(RepositoryIndexError):
    pass


class IndexSafetyError(RepositoryIndexError):
    pass


class IndexLimitError(RepositoryIndexError):
    pass


class SkippedFile(RepositoryIndexError):
    """A file excluded from the index, with a machine-readable reason."""
