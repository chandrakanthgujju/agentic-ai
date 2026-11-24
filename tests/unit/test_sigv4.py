import httpx2
from botocore.credentials import Credentials

from shared.sigv4 import SigV4HttpxAuth


def test_signs_request_with_body_hash():
    auth = SigV4HttpxAuth(lambda: Credentials("AKIDEXAMPLE", "secret", "token"), "us-east-1", "lambda")
    req = httpx2.Request(
        "POST", "https://abc.lambda-url.us-east-1.on.aws/mcp", content=b'{"jsonrpc":"2.0"}', headers={"content-type": "application/json"}
    )
    flow = auth.auth_flow(req)
    signed = next(flow)
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/lambda/aws4_request" in signed.headers["Authorization"]
    assert signed.headers["X-Amz-Security-Token"] == "token"
    assert "X-Amz-Date" in signed.headers
