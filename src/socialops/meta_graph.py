"""Meta Graph API calls for Facebook Pages and Instagram business accounts.

- Token is sent in the Authorization header, never in a URL or log line.
- Errors carry an `ambiguous` flag. A POST that failed without a clear answer (network error,
  timeout, 5xx, 429) may have been applied. The caller must not blindly retry those.
- Transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

GRAPH_HOST = "https://graph.facebook.com"
Transport = Callable[[str, str, dict, dict], tuple[int, dict]]  # method, url, fields, headers


class GraphError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous


class NetworkFailure(OSError):
    pass


def default_transport(method: str, url: str, fields: dict, headers: dict) -> tuple[int, dict]:
    if method == "GET":
        url = f"{url}?{urllib.parse.urlencode(fields)}" if fields else url
        data = None
    else:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {"content-type": "application/x-www-form-urlencoded", **headers}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
        except (ValueError, OSError):
            body = {}
        return exc.code, body
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise NetworkFailure("network error talking to Graph") from exc


class MetaGraph:
    def __init__(self, token: str, api_version: str, transport: Transport = default_transport,
                 host: str = GRAPH_HOST) -> None:
        self._token = token
        self._version = api_version
        self._transport = transport
        self._host = host

    def __repr__(self) -> str:
        return f"MetaGraph(version={self._version}, token=***)"

    def _url(self, path: str) -> str:
        return f"{self._host}/{self._version}/{path.lstrip('/')}"

    def _call(self, method: str, path: str, fields: dict) -> dict:
        headers = {"authorization": f"Bearer {self._token}"}
        try:
            status, body = self._transport(method, self._url(path), fields, headers)
        except NetworkFailure:
            raise GraphError(f"{method} {path}: network failure", ambiguous=(method == "POST")) from None
        if status >= 400 or "error" in body:
            err = body.get("error", {}) if isinstance(body, dict) else {}
            ambiguous = method == "POST" and (status >= 500 or status == 429)
            message = str(err.get("message", ""))[:200]
            raise GraphError(f"{method} {path}: HTTP {status} code {err.get('code')} {message}".strip(),
                             status=status, ambiguous=ambiguous)
        return body

    def get(self, path: str, fields: dict | None = None) -> dict:
        return self._call("GET", path, fields or {})

    def post(self, path: str, fields: dict) -> dict:
        return self._call("POST", path, fields)


# --- publishing primitives -------------------------------------------------

def publish_fb_text(g: MetaGraph, page_id: str, message: str, link: str | None = None) -> str:
    fields = {"message": message}
    if link:
        fields["link"] = link
    return g.post(f"{page_id}/feed", fields)["id"]


def publish_fb_photo(g: MetaGraph, page_id: str, image_url: str, caption: str) -> str:
    body = g.post(f"{page_id}/photos", {"url": image_url, "caption": caption, "published": "true"})
    return body.get("post_id") or body["id"]


def publish_fb_video(g: MetaGraph, page_id: str, video_url: str, description: str) -> str:
    return g.post(f"{page_id}/videos", {"file_url": video_url, "description": description})["id"]


def _wait_for_container(g: MetaGraph, container_id: str, sleep: Callable[[float], None],
                        max_polls: int, interval: float) -> None:
    for _ in range(max_polls):
        status = g.get(container_id, {"fields": "status_code"}).get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise GraphError(f"instagram container {status}")
        sleep(interval)
    raise GraphError("instagram container not ready in time")


def publish_ig_image(g: MetaGraph, ig_id: str, image_url: str, caption: str,
                     sleep: Callable[[float], None] = lambda s: None,
                     max_polls: int = 20, interval: float = 3.0) -> str:
    container = g.post(f"{ig_id}/media", {"image_url": image_url, "caption": caption})["id"]
    _wait_for_container(g, container, sleep, max_polls, interval)
    return g.post(f"{ig_id}/media_publish", {"creation_id": container})["id"]


def publish_ig_reel(g: MetaGraph, ig_id: str, video_url: str, caption: str,
                    sleep: Callable[[float], None] = lambda s: None,
                    max_polls: int = 60, interval: float = 5.0) -> str:
    container = g.post(f"{ig_id}/media", {"media_type": "REELS", "video_url": video_url,
                                           "caption": caption})["id"]
    _wait_for_container(g, container, sleep, max_polls, interval)
    return g.post(f"{ig_id}/media_publish", {"creation_id": container})["id"]
