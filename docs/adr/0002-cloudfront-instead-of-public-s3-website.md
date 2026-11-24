# ADR-0002: Serve the UI from a private S3 bucket through CloudFront

- Status: Accepted
- Date: 2026-11-24

## Context

The request was "UI in S3, open the S3 link in a browser". The literal reading is an S3 static
website endpoint (`http://<bucket>.s3-website-<region>.amazonaws.com`).

## Options

1. **Public S3 website endpoint.** Simplest. But: HTTP only (no TLS); the bucket must allow public
   reads (Block Public Access off); no security headers; and Cognito rejects non-HTTPS callback URLs
   (only `http://localhost` is allowed), so login would not work at all. The browser would also need
   to call API Gateway cross-origin, so the API would need CORS.
2. **Private S3 bucket + CloudFront with Origin Access Control (chosen).** The UI still lives in S3;
   the URL is `https://dxxxx.cloudfront.net`.

## Decision

Option 2. The site bucket blocks all public access and only CloudFront (via OAC, scoped by
`AWS:SourceArn` to this distribution) can read it. CloudFront also routes `/api/*` to API Gateway so
the UI and API share one origin (no CORS on the API), adds a strict Content-Security-Policy, HSTS and
other security headers, and can carry a WAF web ACL.

## Consequences

- Users open the `AppUrl` CloudFront output, not an S3 URL. The README says so plainly.
- First deploy is slower (~5-10 minutes for the distribution).
- CloudFront adds a small per-request cost; within the free tier for personal use.
- API Gateway's own `execute-api` URL is still reachable directly. It still requires a valid Cognito
  token, so this is not an auth bypass, but WAF on CloudFront alone would not cover it. That is why the
  optional WAF (`enableWaf`) is attached to the REST API stage as well as to CloudFront (the
  CloudFront web ACL can only be created from `us-east-1`; other regions get the API ACL only).
