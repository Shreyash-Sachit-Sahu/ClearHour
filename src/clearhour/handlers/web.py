"""Web Lambda (function URL): the dashboard page and its data files, read from the data bucket's site/ prefix.

Only the paths below are served. JSON and HTML go out gzipped when the browser accepts it.
"""

from __future__ import annotations

import base64
import gzip
import os

import boto3
from botocore.exceptions import ClientError

ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8", 300),
    "/index.html": ("index.html", "text/html; charset=utf-8", 300),
    "/data/live.json": ("data/live.json", "application/json; charset=utf-8", 60),
    "/data/replay.json": ("data/replay.json", "application/json; charset=utf-8", 60),
    "/data/alerts.json": ("data/alerts.json", "application/json; charset=utf-8", 5),
    "/data/config.json": ("data/config.json", "application/json; charset=utf-8", 300),
}
_S3 = None


def s3():
    global _S3
    if _S3 is None:
        _S3 = boto3.client("s3")
    return _S3


def _reply(status: int, text: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "text/plain; charset=utf-8"}, "body": text}


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    if method not in ("GET", "HEAD"):
        return _reply(405, "Method not allowed")
    route = ROUTES.get(event.get("rawPath", "/"))
    if not route:
        return _reply(404, "Not found")
    key, content_type, max_age = route
    try:
        body = s3().get_object(Bucket=os.environ["DATA_BUCKET"], Key=f"site/{key}")["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return _reply(404, "Not published yet")
        raise
    headers = {
        "Content-Type": content_type,
        "Cache-Control": f"public, max-age={max_age}",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Vary": "Accept-Encoding",
    }
    if "gzip" in (event.get("headers") or {}).get("accept-encoding", "") and len(body) > 1024:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    return {
        "statusCode": 200,
        "headers": headers,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }
