#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# Licensed under the Apache2.0. See LICENSE file in charm source for details.

"""Low-level HTTP client for the OpenBao API.

This module replaces the ``hvac`` dependency with direct HTTP calls using the
``requests`` library. Error responses are translated into typed exceptions with
the same semantics as ``hvac.utils.raise_for_error``.
"""

import logging
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


class HttpError(Exception):
    """Base class for exceptions raised for OpenBao API error responses."""

    def __init__(
        self,
        message: Optional[str] = None,
        errors: Optional[list] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
        text: Optional[str] = None,
        json: Optional[dict] = None,
        status_code: Optional[int] = None,
    ):
        if errors:
            message = ", ".join(errors)

        self.errors = errors
        self.method = method
        self.url = url
        self.text = text
        self.json = json
        self.status_code = status_code

        super().__init__(message)

    def __str__(self) -> str:
        """Return a string representation including the request context."""
        return f"{self.args[0]}, on {self.method} {self.url}"


class InvalidRequestError(HttpError):
    """Raised when OpenBao responds with HTTP 400."""


class UnauthorizedError(HttpError):
    """Raised when OpenBao responds with HTTP 401."""


class ForbiddenError(HttpError):
    """Raised when OpenBao responds with HTTP 403."""


class InvalidPathError(HttpError):
    """Raised when OpenBao responds with HTTP 404."""


class RateLimitExceededError(HttpError):
    """Raised when OpenBao responds with HTTP 429."""


class InternalServerError(HttpError):
    """Raised when OpenBao responds with HTTP 500."""


class NotInitializedError(HttpError):
    """Raised when OpenBao responds with HTTP 501."""


class BadGatewayError(HttpError):
    """Raised when OpenBao responds with HTTP 502."""


class ServiceUnavailableError(HttpError):
    """Raised when OpenBao responds with HTTP 503."""


class UnexpectedError(HttpError):
    """Raised when OpenBao responds with an unexpected error status code."""


_STATUS_EXCEPTION_MAP = {
    400: InvalidRequestError,
    401: UnauthorizedError,
    403: ForbiddenError,
    404: InvalidPathError,
    429: RateLimitExceededError,
    500: InternalServerError,
    501: NotInitializedError,
    502: BadGatewayError,
    503: ServiceUnavailableError,
}


def raise_for_error(method: str, url: str, response: requests.Response) -> None:
    """Raise a typed exception based on the status code of an error response.

    Extracts the standard ``errors`` list from the response body when present,
    mirroring how hvac surfaces OpenBao/Vault API errors.
    """
    errors = text = None
    body_json: Any = None
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - text decoding should never break error handling
        pass

    if response.headers.get("Content-Type") == "application/json":
        try:
            body_json = response.json()
        except Exception:  # noqa: BLE001 - non-JSON error bodies are passed as text
            pass
        else:
            errors = body_json.get("errors")

    message = None if errors is not None else text
    exception_class = _STATUS_EXCEPTION_MAP.get(response.status_code, UnexpectedError)
    raise exception_class(
        message,
        errors=errors,
        method=method,
        url=url,
        text=text,
        json=body_json,
        status_code=response.status_code,
    )


class OpenBaoHttp:
    """Minimal HTTP client for the OpenBao API, built on requests."""

    def __init__(self, url: str, verify: bool | str = True, timeout: int = 30):
        self.url = url.strip("/")
        self.verify = verify
        self.timeout = timeout
        self._token = ""
        self._session = requests.Session()

    @property
    def token(self) -> str:
        """Return the token used to authenticate with OpenBao."""
        return self._token

    @token.setter
    def token(self, token: str) -> None:
        self._token = token

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Any = None,
        data: Any = None,
        stream: bool = False,
        raise_on_error: bool = True,
    ) -> requests.Response:
        """Send an HTTP request to the OpenBao API.

        Args:
            method: HTTP method to use; "list" maps to the OpenBao LIST verb.
            path: API path, e.g. "/v1/sys/health".
            params: Optional query string parameters.
            json: Optional JSON request body.
            data: Optional raw request body (e.g. snapshot data).
            stream: Whether to stream the response (used for snapshots).
            raise_on_error: Whether to raise an exception for >= 400 status codes.

        Returns:
            The raw requests.Response object.
        """
        url = "/".join((self.url, path.strip("/")))
        headers = {"X-Vault-Request": "true"}
        if self._token:
            headers["X-Vault-Token"] = self._token
        response = self._session.request(
            method,
            url,
            params=params,
            json=json,
            data=data,
            headers=headers,
            stream=stream,
            verify=self.verify,
            timeout=self.timeout,
        )
        if not response.ok and raise_on_error:
            raise_for_error(method, url, response)
        return response

    def get(
        self,
        path: str,
        params: Optional[dict] = None,
        stream: bool = False,
        raise_on_error: bool = True,
    ) -> requests.Response:
        """Send a GET request to the OpenBao API."""
        return self.request(
            "get", path, params=params, stream=stream, raise_on_error=raise_on_error
        )

    def post(
        self,
        path: str,
        json: Any = None,
        data: Any = None,
        raise_on_error: bool = True,
    ) -> requests.Response:
        """Send a POST request to the OpenBao API."""
        return self.request("post", path, json=json, data=data, raise_on_error=raise_on_error)

    def put(self, path: str, json: Any = None, raise_on_error: bool = True) -> requests.Response:
        """Send a PUT request to the OpenBao API."""
        return self.request("put", path, json=json, raise_on_error=raise_on_error)

    def delete(self, path: str, raise_on_error: bool = True) -> requests.Response:
        """Send a DELETE request to the OpenBao API."""
        return self.request("delete", path, raise_on_error=raise_on_error)

    def list(self, path: str, raise_on_error: bool = True) -> requests.Response:
        """Send a LIST request to the OpenBao API."""
        return self.request("list", path, raise_on_error=raise_on_error)

    def list_get_fallback(self, path: str, raise_on_error: bool = True) -> requests.Response:
        """Send a GET request with list=true, as an alternative to the LIST verb."""
        return self.request("get", path, params={"list": "true"}, raise_on_error=raise_on_error)
