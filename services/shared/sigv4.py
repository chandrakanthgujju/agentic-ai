"""SigV4 request signing for httpx2 (the HTTP client used by the MCP SDK).

The MCP server is a Lambda Function URL with ``AuthType=AWS_IAM``: only
principals whose IAM policy allows ``lambda:InvokeFunctionUrl`` on that function
can call it. The agent signs every MCP HTTP request with its execution-role
credentials, so there is no shared secret or API key to rotate.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx2
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

# Headers that hop-by-hop proxies may rewrite; signing them breaks verification.
_UNSIGNED = {"connection", "accept-encoding", "user-agent", "content-length", "expect"}


class SigV4HttpxAuth(httpx2.Auth):
    requires_request_body = True

    def __init__(self, credentials_provider, region: str, service: str = "lambda"):
        self._credentials_provider = credentials_provider
        self._region = region
        self._service = service

    def _credentials(self) -> Credentials:
        creds = self._credentials_provider()
        # botocore RefreshableCredentials refresh on access; freeze for one signature.
        return creds.get_frozen_credentials() if hasattr(creds, "get_frozen_credentials") else creds

    def sign(self, request: httpx2.Request) -> None:
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _UNSIGNED}
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content or b"",
            headers=headers,
        )
        SigV4Auth(self._credentials(), self._service, self._region).add_auth(aws_request)
        for key in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
            if key in aws_request.headers:
                request.headers[key] = aws_request.headers[key]

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        self.sign(request)
        yield request
