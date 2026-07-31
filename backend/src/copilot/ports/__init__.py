"""Port interfaces (Protocols) the services depend on.

The domain and services import only these; concrete I/O lives in ``adapters`` and
is injected at the edges. This keeps business logic testable with fakes.

``JobSourcePort`` went with the adapter that implemented it. It typed a posting as
a raw ``Mapping[str, str]``, which is exactly what let a bundled 4-row fixture of
invented companies satisfy the port and reach the briefing as real matches. Supply
now arrives as validated :class:`~copilot.domain.posting.Posting` objects through
:class:`PostingSourcePort`, so that class of bug no longer type-checks.
"""

from copilot.ports.interpreter import Confidence, Interpretation, InterpreterPort
from copilot.ports.llm import LLMPort
from copilot.ports.mailbox import MailboxPort
from copilot.ports.postingsource import PostingSourcePort
from copilot.ports.postingstore import PostingStorePort
from copilot.ports.secrets import SecretsPort
from copilot.ports.store import StorePort

__all__ = [
    "Confidence",
    "Interpretation",
    "InterpreterPort",
    "LLMPort",
    "MailboxPort",
    "PostingSourcePort",
    "PostingStorePort",
    "SecretsPort",
    "StorePort",
]
