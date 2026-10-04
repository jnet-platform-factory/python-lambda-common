"""Keep credentials out of CloudWatch.

Lambda stdout goes straight to a log group, where it sits for the retention period and is
readable by anyone with logs access. A bearer token printed there is a usable credential for as
long as it is valid, and an API key is usable until someone rotates it -- so the printing, not
the storing, is the vulnerability.

Debugging still needs *something*, and "no output at all" is what makes people add the print
back. So these helpers keep the parts that are safe and useful -- which scheme was used, how
long the value was, whether two requests carried the same token -- and drop only the secret
itself.
"""

import hashlib
import re

## Header names whose value is a credential. Matched case-insensitively, since API Gateway and
## the various clients disagree about capitalisation (`Authorization`, `authorization`,
## `X-Webhook-Token`, `x-webhook-token` all turn up in practice).
SENSITIVE_HEADERS = frozenset({
    "authorization",
    "proxy-authorization",
    "authentication",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "x-webhook-token",
    "x-amz-security-token",
    "x-csrf-token",
})

## Substrings that make a mapping key's value a credential -- for auth payloads, decoded JWTs
## and connector settings. Matched case-insensitively *anywhere in the key*, deliberately: an
## exact-match list only redacts the spellings someone happened to think of. Over-redacting a
## field called `token_count` is a worse log line; under-redacting one is a leaked credential.
SENSITIVE_KEY_STEMS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "auth",
    "private_key",
)

## Keys that contain a stem but are not secrets. Checked first.
SENSITIVE_KEY_EXCEPTIONS = frozenset({
    "token_count",
    "tokens_used",
    "prompt_token_count",
    "candidates_token_count",
    "total_token_count",
    "auth_type",
    "authenticated",
})

REDACTED = "<redacted>"


def is_sensitive_key(key):
    """Does this mapping key hold a credential?"""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if lowered in SENSITIVE_KEY_EXCEPTIONS:
        return False
    return any(stem in lowered for stem in SENSITIVE_KEY_STEMS)


def token_fingerprint(token):
    """A stable, non-reversible stand-in for a token.

    `sha256:1f3a9c2d len=812` is enough to tell whether two requests carried the same token, or
    whether the token changed after a refresh, which is what you actually want a token in the
    logs for. It does not let anyone replay it.
    """
    if token is None:
        return None
    if not isinstance(token, str):
        token = str(token)
    if not token:
        return "<empty>"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    return f"sha256:{digest} len={len(token)}"


def redact_header_value(name, value):
    """Redact one header, keeping the auth scheme because it is diagnostic and not secret."""
    if not isinstance(name, str) or name.lower() not in SENSITIVE_HEADERS:
        return value
    if not isinstance(value, str) or not value:
        return REDACTED

    scheme, _, credential = value.partition(" ")
    ## `Bearer eyJ...` / `ApiKey abc...` / `Basic dXNl...` -- which scheme was used is often the
    ## whole question when auth misbehaves, and it identifies nobody.
    if credential:
        return f"{scheme} {token_fingerprint(credential)}"
    return token_fingerprint(value)


def redact_headers(headers):
    """A copy of a header mapping safe to print. Non-sensitive headers pass through."""
    if not isinstance(headers, dict):
        return headers
    return {name: redact_header_value(name, value) for name, value in headers.items()}


def redact_multi_value_headers(headers):
    """`redact_headers` for API Gateway's `multiValueHeaders`, where each value is a list."""
    if not isinstance(headers, dict):
        return headers
    return {
        name: [redact_header_value(name, v) for v in values] if isinstance(values, list)
        else redact_header_value(name, values)
        for name, values in headers.items()
    }


def redact_mapping(data, _depth=0):
    """A copy of a mapping with credential-valued keys replaced, recursing into nested ones.

    Used for auth payloads and decoded JWT claims, where the secret is a value rather than a
    header. Depth is capped so a self-referential dict cannot spin here.
    """
    if _depth > 6 or not isinstance(data, dict):
        return data
    redacted = {}
    for key, value in data.items():
        if is_sensitive_key(key):
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = redact_mapping(value, _depth + 1)
        elif isinstance(value, list):
            redacted[key] = [redact_mapping(v, _depth + 1) if isinstance(v, dict) else v
                             for v in value]
        else:
            redacted[key] = value
    return redacted


## `scheme://user:password@host/...` -- SQLAlchemy URLs, AMQP URLs, anything urllib-shaped.
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z0-9+.\-]+://)(?P<user>[^:/@\s]+):(?P<pw>[^@/\s]+)@")


def redact_url_credentials(url):
    """Strip the password out of a connection URL, keeping everything you need to read it.

    `postgresql://svc:hunter2@host:5432/db` -> `postgresql://svc:<redacted>@host:5432/db`.
    The host, port, database and user are the diagnostic part; the password never is.
    """
    if not isinstance(url, str):
        return url
    return _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:{REDACTED}@", url)


def redact_event(event):
    """A copy of an API Gateway proxy event safe to log: credential headers fingerprinted."""
    if not isinstance(event, dict):
        return event
    redacted = dict(event)
    if "headers" in redacted:
        redacted["headers"] = redact_headers(redacted.get("headers"))
    if "multiValueHeaders" in redacted:
        redacted["multiValueHeaders"] = redact_multi_value_headers(redacted.get("multiValueHeaders"))
    return redacted
