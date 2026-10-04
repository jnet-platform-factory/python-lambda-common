import json
import time
from types import SimpleNamespace

import jwt

SECRET = "test-secret"


def lambda_context(name="svc-fn"):
    return SimpleNamespace(function_name=name, function_version="$LATEST", memory_limit_in_mb=128,
                           invoked_function_arn=f"arn:aws:lambda:us-east-1:000000000000:function:{name}",
                           aws_request_id="req-1", log_group_name="g", log_stream_name="s",
                           get_remaining_time_in_millis=lambda: 1000)


def proxy_event(method="GET", path="/things", headers=None, claims=None, authorizer=None, body=None,
                query=None):
    request_context = {"resourcePath": path, "httpMethod": method, "stage": "dev", "requestId": "r-1"}
    if claims is not None:
        request_context["authorizer"] = {"claims": claims}
    elif authorizer is not None:
        request_context["authorizer"] = authorizer
    return {
        "resource": path,
        "path": path,
        "httpMethod": method,
        "headers": headers or {},
        "multiValueHeaders": {k: [v] for k, v in (headers or {}).items()},
        "queryStringParameters": query,
        "requestContext": request_context,
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }


def hs256(payload, secret=SECRET, expires_in=600):
    return jwt.encode({**payload, "exp": int(time.time()) + expires_in}, secret, algorithm="HS256")


def rs256_shaped(payload):
    """A token with an RS256 header. Its signature is never checked by the middleware."""
    header = {"alg": "RS256", "typ": "JWT"}
    import base64

    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64(header)}.{b64(payload)}.c2ln"


def body_of(response):
    return json.loads(response["body"]) if response.get("body") else None
