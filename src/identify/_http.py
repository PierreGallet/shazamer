"""A Shazam HTTP client that says *why* a request failed.

shazamio's own client funnels every non-JSON response into one
`FailedDecodeJson("Failed to decode json")`. That is the wrong shape for the
one failure that matters here: Shazam answers a rate-limited request with
`HTTP 429` and a 142-byte HTML page, so the whole rate-limit signal arrives
disguised as a parsing problem.

The cost of that disguise was measured rather than guessed. In one production
run 85 of 128 probes ended this way, and because the caller could not tell a
refusal from a bad answer, it retried each one four times — which is precisely
what keeps a rate limit alive.

This subclass reads the status before the body and raises something the caller
can act on.
"""
from __future__ import annotations

from typing import Any, Dict, List, Union

from aiohttp_retry import RetryClient
from shazamio.client import HTTPClient
from shazamio.exceptions import BadMethod
from shazamio.utils import validate_json


class RateLimited(Exception):
    """Shazam refused the request outright. Not an answer about the audio."""


class ShazamHTTPClient(HTTPClient):
    """shazamio's client, plus a distinct exception for 429."""

    async def request(self, method: str, url: str, *args,
                      **kwargs) -> Union[List[Any], Dict[str, Any]]:
        async with RetryClient(
            retry_options=self.retry_options,
            raise_for_status=False,
            trace_configs=[self.trace_config],
        ) as client:
            verb = method.upper()
            if verb not in ("GET", "POST"):
                raise BadMethod("Accept only GET/POST")

            send = client.get if verb == "GET" else client.post
            async with send(url, **kwargs) as resp:
                # Checked before the body is touched: a 429 carries HTML, and
                # letting validate_json see it first is how the status got
                # lost in the first place.
                if resp.status == 429:
                    raise RateLimited("Shazam returned 429 Too Many Requests")
                return await validate_json(resp, *args)
