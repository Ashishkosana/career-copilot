"""Port for resolving a credential that must not live in code, git, or a template.

Three consumers need a credential — the reply drafter, the level interpreter, and
the Gmail mailbox — and until this port existed each of them could only read a
direct API key handed in by the caller, which in the cloud meant no credential at
all. One seam rather than three ``boto3`` calls buys three things:

**One place where "absent" is defined.** The whole product is keyless by design:
job supply is five unauthenticated ATS APIs and the gates are regex, so a
credential lookup that raises would take down a sweep that never needed a
credential. Absence is therefore a *return value* here — ``""`` or ``None``, never
an exception — and that rule is only enforceable if there is a single
implementation to enforce it in.

**One fake.** Every consumer's degradation path is testable with an in-memory
double and no AWS, which is also how the test suite and local dev run.

**No vendor in the consumers.** ``adapters/ssm_secrets.py`` knows about SSM
Parameter Store and Secrets Manager; the three consumers know only these two
methods, and neither method name mentions a store.

The split is by the *shape* of the credential, not by which store holds it. A
single opaque string and a JSON document need different validation, and choosing
the store for each is the adapter's business (see its module docstring for why the
API keys live in SSM and the Gmail token lives in Secrets Manager).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class SecretsPort(Protocol):
    """Resolve credentials by name. Nothing here raises, and nothing logs a value."""

    def api_key(self, *, parameter_name: str, env_var: str = "") -> str:
        """A single-string credential, or ``""`` when there is none.

        ``env_var`` names an environment variable that **wins** over
        ``parameter_name`` when it is set and non-empty — that is what makes local
        dev and the test suite work with no AWS account. Implementations must
        treat a missing name, a missing credential, an empty value, and a refused
        or failed lookup as the same answer: ``""``.
        """
        ...

    def secret_json(self, secret_id: str) -> Mapping[str, str] | None:
        """A JSON-document credential, or ``None`` when there is none.

        String-valued fields only. A document that cannot be parsed, holds no
        usable fields, or cannot be read is ``None`` — the caller then behaves
        exactly as it does when the credential was never configured.
        """
        ...
