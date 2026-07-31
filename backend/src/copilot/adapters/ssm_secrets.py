"""AWS implementation of :class:`~copilot.ports.secrets.SecretsPort`.

This is the runtime half of a wiring whose infra half already shipped. The stack
sets ``COPILOT_GMAIL_SECRET_ID``, ``COPILOT_LLM_SECRET_ID`` and
``COPILOT_INTERPRETER_SECRET_ID``, grants ``secretsmanager:GetSecretValue`` on one
secret and ``ssm:GetParameter`` on exactly two parameter ARNs — and until this
module existed **nothing read any of it**. ``Settings`` exposed the three ids and
no code resolved one, so the only credential path that worked was a
``COPILOT_*_API_KEY`` environment variable, which no Lambda is given. That is why
the deployed inbox half has reported ``inbox_ok: false`` every day and why the
level interpreter, fully built and fully tested, has run in no pipeline at all.

**Two stores, deliberately.**

*SSM Parameter Store (SecureString)* holds the two API keys. CloudFormation cannot
create a SecureString parameter, and both alternatives leak the key into the
synthesised template — a plaintext ``String`` parameter and a Lambda env var are
equally visible in ``cdk.out`` and in the CloudFormation console. So the stack
passes the parameter *name*, grants a read on that exact ARN, and a human creates
the parameter out of band. SecureString is also free at this volume, against
$0.40/secret/month for a static string that never rotates.

*Secrets Manager* holds ``career-copilot/gmail``: an OAuth refresh token, the one
credential here with a lifecycle (it can be revoked, and re-granting it is a
browser consent flow, not an edit). The stack creates it empty with
``RemovalPolicy.RETAIN`` so a stack delete cannot take the authorisation with it.

**Absence is a supported state, in every direction.** A missing parameter, an
empty value, ``AccessDenied``, throttling, no boto3 in the bundle, an unreachable
endpoint — all of them resolve to "no credential", and every consumer already has
a defined behaviour for that: the interpreter returns ``None`` and its caller keeps
the deterministic verdict, the drafter returns ``""``, the mailbox stays
unconfigured and ``inbox_ok`` says so. This is not politeness. The product's supply
half is five unauthenticated ATS APIs and regex gates — 25,294 postings, no key
required — and a credential lookup that raised would take the whole sweep down to
protect a tier that is optional by design. **Nothing in this module raises.**

**Nothing about a value is ever logged.** Names only: not the value, not its
length, not a prefix, not a masked form. A length narrows a brute-force search and
a prefix identifies the provider and the key type, and anything that reaches
CloudWatch cannot be un-leaked from it — log retention, metric filters and
Logs Insights queries all copy it forward. The parameter name is enough to fix any
problem this module can have.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from copilot.logging import get_logger

_LOG = get_logger("copilot.adapters.ssm_secrets")

#: Error codes that mean "this credential was never set up" — the *expected* state
#: for both optional API keys, so they are logged at INFO. Everything else
#: (``AccessDeniedException``, ``ThrottlingException``, a transport error) is a
#: WARNING: it still degrades to "no credential", but it is a misconfiguration or
#: an outage rather than a deliberate absence, and the two must not look alike in
#: the logs of a run whose interpreter silently did nothing.
_ABSENT_CODES = frozenset(
    {
        "ParameterNotFound",
        "ParameterVersionNotFound",
        "ResourceNotFoundException",
    }
)

#: Bounded on purpose. The default botocore socket timeout is 60s; three of those
#: on an unreachable endpoint would spend minutes of a cron's budget resolving
#: credentials it can run without. A credential lookup gets seconds, then the run
#: proceeds keyless.
_CONNECT_TIMEOUT_S = 2
_READ_TIMEOUT_S = 4
_MAX_ATTEMPTS = 3


class AwsSecrets:
    """SecretsPort over SSM Parameter Store and Secrets Manager (boto3 lazy).

    Named for AWS rather than for SSM alone because it spans both stores; the
    module keeps the name the infra comments already point at.

    **Cache lifetime is this object's lifetime, and nothing longer.**
    ``GetParameter`` and ``GetSecretValue`` are network calls; one cron invocation
    resolves up to three credentials, and the same name can be asked for by more
    than one consumer or by an adapter rebuilt part-way through a run — so a
    per-instance dict removes the repeats *within* a run. It is deliberately not a
    module-level or class-level cache: a Lambda execution environment is reused
    across many invocations for hours, so a process-lifetime cache would keep
    serving a key that has since been rotated, revoked or created, and "when would
    it notice?" would have no answer anyone could reason about. Build one of these
    per handler invocation — never one per module — and the guarantee is exact:
    **fresh at the start of every run, at most one lookup per name within it.**
    Rotating a key then takes effect on the next run, at most a day later, with no
    deploy and no cache to reason about.

    Absence is cached too. A missing parameter is the normal state for both
    optional keys, and re-asking three times in one run for a parameter that does
    not exist wastes exactly the same round trip as re-asking for one that does.

    Pass ``ssm``/``secrets_manager`` to drive this with a fake — that is how the
    tests cover every degradation path with no network and no credentials.
    """

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        ssm: Any | None = None,
        secrets_manager: Any | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._region = region
        self._clients: dict[str, Any] = {}
        if ssm is not None:
            self._clients["ssm"] = ssm
        if secrets_manager is not None:
            self._clients["secretsmanager"] = secrets_manager
        #: ``None`` means "read the live environment at call time", so a test that
        #: sets an env var after construction still sees it.
        self._env = env
        self._keys: dict[str, str] = {}
        self._documents: dict[str, Mapping[str, str] | None] = {}

    # --- port ----------------------------------------------------------------

    def api_key(self, *, parameter_name: str, env_var: str = "") -> str:
        """Resolve a single-string credential, or ``""`` when there is none.

        Resolution order, and the order is the whole point:

        1. **``env_var`` in the environment**, when a name is given. A direct
           ``COPILOT_*_API_KEY`` beats the secret store so that a laptop, a
           one-off script and the test suite all work with no AWS account and no
           IAM role — and so that reproducing a cloud problem locally does not
           require read access to production credentials.
        2. **The SSM SecureString** named ``parameter_name``, cached per instance.
        3. **``""``** — no credential. Never an exception.
        """
        if env_var:
            direct = self._environ().get(env_var, "").strip()
            if direct:
                return direct
        if not parameter_name:
            return ""
        cached = self._keys.get(parameter_name)
        if cached is not None:
            return cached
        resolved = self._fetch_parameter(parameter_name)
        self._keys[parameter_name] = resolved
        return resolved

    def secret_json(self, secret_id: str) -> Mapping[str, str] | None:
        """Resolve a JSON-document credential, or ``None`` when there is none."""
        if not secret_id:
            return None
        if secret_id in self._documents:
            return self._documents[secret_id]
        resolved = self._fetch_document(secret_id)
        self._documents[secret_id] = resolved
        return resolved

    # --- SSM -----------------------------------------------------------------

    def _fetch_parameter(self, name: str) -> str:
        client = self._client("ssm")
        if client is None:
            return ""
        try:
            response = client.get_parameter(Name=name, WithDecryption=True)
        except Exception as exc:
            # Deliberately broad, and the reason is the product's shape rather
            # than laziness: every distinguishable failure here has the same
            # correct handling, and the alternative — naming botocore's
            # dynamically-built exception classes — needs botocore imported at
            # module scope, which is exactly the lazy import that keeps this
            # package importable on a machine with no AWS SDK.
            self._note_unavailable("parameter", name, exc)
            return ""
        value = _parameter_value(response)
        if not value:
            # A SecureString that exists and holds nothing usable. Treated as
            # absent, and said out loud, because "the key is set but the tier did
            # nothing" is otherwise indistinguishable from "no key".
            _LOG.info(
                "secret_empty", extra={"extra_fields": {"store": "parameter", "name": name}}
            )
            return ""
        _LOG.info("secret_resolved", extra={"extra_fields": {"store": "parameter", "name": name}})
        return value

    # --- Secrets Manager -----------------------------------------------------

    def _fetch_document(self, secret_id: str) -> Mapping[str, str] | None:
        client = self._client("secretsmanager")
        if client is None:
            return None
        try:
            response = client.get_secret_value(SecretId=secret_id)
        except Exception as exc:  # see _fetch_parameter for why this is broad
            self._note_unavailable("secret", secret_id, exc)
            return None
        raw = response.get("SecretString") if isinstance(response, Mapping) else None
        if not isinstance(raw, str) or not raw.strip():
            # The stack creates this secret empty and a human seeds it, so
            # "exists but holds nothing" is a state that really occurs. Binary
            # secrets are not supported and land here too: an OAuth token is JSON.
            _LOG.info(
                "secret_empty", extra={"extra_fields": {"store": "secret", "name": secret_id}}
            )
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            _LOG.warning(
                "secret_not_json", extra={"extra_fields": {"store": "secret", "name": secret_id}}
            )
            return None
        fields = _string_fields(parsed)
        if not fields:
            _LOG.warning(
                "secret_no_usable_fields",
                extra={"extra_fields": {"store": "secret", "name": secret_id}},
            )
            return None
        _LOG.info(
            "secret_resolved",
            extra={"extra_fields": {"store": "secret", "name": secret_id, "fields": len(fields)}},
        )
        return fields

    # --- plumbing ------------------------------------------------------------

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _client(self, service: str) -> Any | None:
        """A cached boto3 client, or ``None`` if the SDK is not installed.

        boto3 is imported inside the method, like every other adapter here, so
        importing ``copilot`` on a machine with no AWS SDK still works — and a
        hand-assembled Lambda bundle that is missing the wheel degrades to "no
        credential" instead of failing at import time, which in Lambda means the
        whole handler never runs.
        """
        existing = self._clients.get(service)
        if existing is not None:
            return existing
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            _LOG.warning("secrets_sdk_missing", extra={"extra_fields": {"service": service}})
            return None
        client = boto3.client(
            service,
            region_name=self._region,
            config=Config(
                retries={"mode": "standard", "max_attempts": _MAX_ATTEMPTS},
                connect_timeout=_CONNECT_TIMEOUT_S,
                read_timeout=_READ_TIMEOUT_S,
            ),
        )
        self._clients[service] = client
        return client

    @staticmethod
    def _note_unavailable(store: str, name: str, exc: Exception) -> None:
        """Log a failed lookup by NAME and error code. Never a value, ever.

        There is no value to leak on this path — the call failed — but the log
        event is shared with the caller's mental model of "what happened to my
        key", so it carries the two facts that can fix it: which name was asked
        for, and what AWS said. ``ParameterNotFound`` is INFO because it is the
        documented steady state of an optional tier; anything else is a WARNING.
        """
        code = _error_code(exc)
        fields = {"store": store, "name": name, "code": code}
        if code in _ABSENT_CODES:
            _LOG.info("secret_absent", extra={"extra_fields": fields})
        else:
            _LOG.warning("secret_unavailable", extra={"extra_fields": fields})


def _error_code(exc: Exception) -> str:
    """The AWS error code for ``exc``, falling back to its class name.

    Read out of ``response["Error"]["Code"]`` rather than by catching botocore's
    typed exceptions: those classes are built dynamically per client, so naming
    them would force botocore into module scope and make the adapter untestable
    against an in-memory double. The class-name fallback covers the errors that
    carry no response at all (``EndpointConnectionError``, ``NoCredentialsError``).
    """
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if isinstance(code, str) and code:
                return code
    return type(exc).__name__


def _parameter_value(response: Any) -> str:
    """The decrypted value out of a ``GetParameter`` response, or ``""``.

    Total rather than indexing: a response shape that surprises us must degrade to
    "no credential" like everything else here. The ``strip`` is not cosmetic —
    ``put-parameter --value "$(cat key.txt)"`` stores a trailing newline, and a
    newline inside an ``Authorization`` header fails as a malformed request rather
    than as a bad key, which is a genuinely confusing hour.
    """
    if not isinstance(response, Mapping):
        return ""
    parameter = response.get("Parameter")
    if not isinstance(parameter, Mapping):
        return ""
    value = parameter.get("Value")
    return value.strip() if isinstance(value, str) else ""


def _string_fields(parsed: Any) -> dict[str, str]:
    """String-valued fields of a parsed JSON object; ``{}`` for anything else.

    Non-string values are dropped rather than coerced or passed through. A
    credential constructor handed a dict where it expects a token fails deep
    inside a vendor SDK with a message about neither the field nor the secret, and
    filtering at this boundary is what keeps the port's ``Mapping[str, str]``
    contract true instead of merely annotated.
    """
    if not isinstance(parsed, dict):
        return {}
    return {str(k): v for k, v in parsed.items() if isinstance(v, str) and v.strip()}
