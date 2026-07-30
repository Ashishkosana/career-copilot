from __future__ import annotations

from typing import Protocol

from copilot.domain.posting import Posting


class PostingSourcePort(Protocol):
    """Fetch open roles from one job-board source.

    Implementations must not raise on a single bad tenant — a source that cannot
    be reached returns an empty list and logs, so one dead board never sinks a run.
    """

    name: str

    def fetch(self) -> list[Posting]: ...
