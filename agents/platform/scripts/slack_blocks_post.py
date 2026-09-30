"""Post a Block Kit message to Slack from a process outside the gateway.

``hermes send`` takes text and no blocks, so a caller that has buttons to show
(the fleet-audit relay in ``session_kv_server``, the first inventory in
``bootstrap_delivery``) posts ``chat.postMessage`` itself, through the
credential proxy's Slack relay at ``SLACK_RELAY_URL``: the call the relay
patch's standalone sender makes, with the same authentication. With no relay
configured nothing is posted and the caller keeps its text path.

:func:`post` returns the message ts. It raises :class:`Refused` when Slack
itself rejected the message, which for a well-formed call means the blocks,
so the caller can retry with less or fall back to text; anything else it
raises is the relay failing.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from credential_proxy_client import authorization_headers
from slack_relay_patch import relayed_slack_error

RELAY_ENV = "SLACK_RELAY_URL"
RELAY_API_PATH = "/v1/chat/slack/api"
POST_METHOD = "chat.postMessage"
#: The relay patch's own bound on one relayed call.
POST_TIMEOUT_S = 35
UNKNOWN_ERROR = "unknown"


class NotConfigured(RuntimeError):
    """No Slack relay in this process's environment."""


class Refused(RuntimeError):
    """Slack rejected the message; ``str()`` is Slack's error code."""


def configured() -> bool:
    return bool(os.environ.get(RELAY_ENV, "").strip())


def post(channel: str, text: str, blocks: list[dict] | None, thread_ts: str = "") -> str:
    """Post ``blocks`` with ``text`` as the notification and fallback; return the message ts.

    With no ``blocks`` the message is ``text`` alone. Returning at all means
    the message posted, even with an empty ts.
    """
    relay_url = os.environ.get(RELAY_ENV, "").strip().rstrip("/")
    if not relay_url:
        raise NotConfigured(RELAY_ENV)
    body: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        body["blocks"] = blocks
    if thread_ts:
        body["thread_ts"] = thread_ts
    # An empty team falls through to the proxy's primary client, as in the relay patch.
    payload = {"teamId": "", "method": POST_METHOD, "arguments": {"json": body}}
    request = urllib.request.Request(
        relay_url + RELAY_API_PATH,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **authorization_headers()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_S) as response:
            data = (json.load(response) or {}).get("response") or {}
    except urllib.error.HTTPError as exc:
        fields = relayed_slack_error(exc)
        if fields is None:
            raise
        raise Refused(str(fields.get("error") or UNKNOWN_ERROR)) from exc
    if not data.get("ok"):
        raise Refused(str(data.get("error") or UNKNOWN_ERROR))
    # An ok without a ts is still a message in the channel, not one to send again.
    return str(data.get("ts") or "")
