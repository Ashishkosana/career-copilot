"""Concrete I/O adapters implementing the port Protocols.

Every cloud/vendor SDK is imported *lazily* inside the method that needs it, so
importing this package (e.g. in tests or the domain) never drags in boto3 or the
Google client. The domain and services depend only on ``ports``; these classes
are wired in at the edges (handlers) or replaced by fakes in tests.
"""

from copilot.adapters.claude_interpreter import ClaudeInterpreter
from copilot.adapters.dynamodb_posting_store import (
    OPEN_INDEX_PROJECTION,
    DynamoDbPostingStore,
    PostingTooLargeError,
)
from copilot.adapters.dynamodb_store import DynamoDbStore
from copilot.adapters.gmail_mailbox import GmailMailbox
from copilot.adapters.llm_reply import LlmReplyDrafter
from copilot.adapters.sqlite_posting_store import SqlitePostingStore
from copilot.adapters.ssm_secrets import AwsSecrets

__all__ = [
    "OPEN_INDEX_PROJECTION",
    "AwsSecrets",
    "ClaudeInterpreter",
    "DynamoDbPostingStore",
    "DynamoDbStore",
    "GmailMailbox",
    "LlmReplyDrafter",
    "PostingTooLargeError",
    "SqlitePostingStore",
]
