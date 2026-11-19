"""Minimal RFC 6455 WebSocket client for Binance public/private streams.

Only the client side is implemented, with mandatory TLS verification for ``wss``.
``ws`` is accepted solely for loopback tests when explicitly allowed by config.
Control frames, fragmentation and close handling are fail-closed and bounded.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from dataclasses import dataclass
from typing import Callable

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_HEADER_BYTES = 64 * 1024


class WebSocketError(RuntimeError):
    """Handshake, framing or transport failure."""


@dataclass(frozen=True)
class WebSocketConfig:
    connect_timeout_s: float = 10.0
    recv_timeout_s: float = 180.0
    max_message_bytes: int = 2_000_000
    allow_insecure_ws: bool = False

    def __post_init__(self) -> None:
        for value, name in ((self.connect_timeout_s, "connect_timeout_s"),
                            (self.recv_timeout_s, "recv_timeout_s")):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise WebSocketError(f"{name} must be positive")
        if not isinstance(self.max_message_bytes, int) or self.max_message_bytes <= 0:
            raise WebSocketError("max_message_bytes must be positive")
        if not isinstance(self.allow_insecure_ws, bool):
            raise WebSocketError("allow_insecure_ws must be boolean")


class WebSocketConnection:
    """Blocking client connection; one text message per ``recv_text`` call."""

    def __init__(
        self,
        url: str,
        *,
        config: WebSocketConfig | None = None,
        socket_factory: Callable[[tuple[str, int], float], socket.socket] | None = None,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.config = config or WebSocketConfig()
        self._url = self._parse_url(url)
        self._socket_factory = socket_factory or socket.create_connection
        self._tls_context = tls_context or ssl.create_default_context()
        self._sock: socket.socket | None = None
        self._fragments: list[bytes] = []
        self._fragment_opcode: int | None = None
        self._fragment_size = 0
        self._close_sent = False

    @property
    def url(self) -> str:
        return self._url

    @staticmethod
    def _parse_url(url: str) -> str:
        if not isinstance(url, str) or not url:
            raise WebSocketError("websocket url is required")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError("websocket url must use ws or wss")
        if not parsed.hostname:
            raise WebSocketError("websocket url has no host")
        if parsed.username is not None or parsed.password is not None:
            raise WebSocketError("websocket url must not contain credentials")
        return url

    def _parts(self) -> tuple[str, str, int, str]:
        parsed = urllib.parse.urlsplit(self._url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return parsed.scheme, host, port, path

    def connect(self) -> None:
        if self._sock is not None:
            raise WebSocketError("websocket is already connected")
        scheme, host, port, path = self._parts()
        if scheme == "ws":
            if not self.config.allow_insecure_ws or host not in _LOOPBACK_HOSTS:
                raise WebSocketError("insecure ws is allowed only for explicit loopback tests")
        if scheme == "wss":
            context = self._tls_context
            if context is None:
                raise WebSocketError("wss requires a TLS context")
        try:
            sock = self._socket_factory((host, port), self.config.connect_timeout_s)
        except OSError as exc:
            raise WebSocketError(f"websocket connect failed: {exc}") from exc
        try:
            sock.settimeout(self.config.connect_timeout_s)
            if scheme == "wss":
                sock = self._tls_context.wrap_socket(sock, server_hostname=host)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            host_header = host if port == (443 if scheme == "wss" else 80) else f"{host}:{port}"
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            ).encode("ascii")
            sock.sendall(request)
            header = self._read_headers(sock)
            self._validate_handshake(header, key)
            sock.settimeout(self.config.recv_timeout_s)
            self._sock = sock
            self._fragments = []
            self._fragment_opcode = None
            self._fragment_size = 0
            self._close_sent = False
        except Exception:
            try:
                sock.close()
            except OSError:
                pass
            raise

    def _read_headers(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) > _MAX_HEADER_BYTES:
                raise WebSocketError("websocket handshake headers are too large")
            chunk = sock.recv(1)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _validate_handshake(header: bytes, key: str) -> None:
        try:
            text = header.decode("iso-8859-1")
        except UnicodeDecodeError as exc:
            raise WebSocketError("handshake is not latin-1") from exc
        lines = text.split("\r\n")
        if not lines or len(lines[0].split(" ")) < 2 or lines[0].split(" ")[1] != "101":
            raise WebSocketError("websocket upgrade was not accepted")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            if ":" not in line:
                raise WebSocketError("malformed handshake header")
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() != "websocket":
            raise WebSocketError("handshake upgrade header is missing")
        if "upgrade" not in headers.get("connection", "").lower():
            raise WebSocketError("handshake connection header is missing")
        expected = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketError("handshake accept key mismatch")
        if headers.get("sec-websocket-extensions"):
            raise WebSocketError("unsupported websocket extension selected")

    def _read_exact(self, size: int) -> bytes:
        if self._sock is None:
            raise WebSocketError("websocket is not connected")
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self._sock.recv(size - len(chunks))
            if not chunk:
                raise WebSocketError("connection closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("websocket is not connected")
        length = len(payload)
        header = bytearray()
        header.append(0x80 | (opcode & 0x0F))
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketError("websocket RSV bits are set")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        if masked:
            raise WebSocketError("server frames must not be masked")
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > self.config.max_message_bytes:
            raise WebSocketError("websocket frame exceeds the configured limit")
        if opcode >= 0x8 and (not fin or length > 125):
            raise WebSocketError("invalid websocket control frame")
        return fin, opcode, self._read_exact(length)

    def recv_text(self) -> str:
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:
                if not self._close_sent:
                    self._send_frame(0x8, b"")
                    self._close_sent = True
                self.close()
                raise WebSocketError("websocket closed by peer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if self._fragment_opcode is not None:
                    raise WebSocketError("nested websocket data frame")
                if fin:
                    return self._finish_message(opcode, payload)
                self._fragment_opcode = opcode
                self._fragments = [payload]
                self._fragment_size = len(payload)
                continue
            if opcode == 0x0:
                if self._fragment_opcode is None:
                    raise WebSocketError("unexpected websocket continuation")
                self._fragment_size += len(payload)
                if self._fragment_size > self.config.max_message_bytes:
                    raise WebSocketError("websocket message exceeds the configured limit")
                self._fragments.append(payload)
                if fin:
                    complete = b"".join(self._fragments)
                    message_opcode = self._fragment_opcode
                    self._fragments = []
                    self._fragment_opcode = None
                    self._fragment_size = 0
                    return self._finish_message(message_opcode, complete)
                continue
            raise WebSocketError(f"unsupported websocket opcode {opcode}")

    @staticmethod
    def _finish_message(opcode: int, payload: bytes) -> str:
        if opcode != 0x1:
            raise WebSocketError("binary websocket messages are not supported")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WebSocketError("websocket text is not valid UTF-8") from exc

    def send_text(self, text: str) -> None:
        if not isinstance(text, str) or not text:
            raise WebSocketError("text message is required")
        self._send_frame(0x1, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        if not isinstance(payload, bytes) or len(payload) > 125:
            raise WebSocketError("ping payload must be bytes of at most 125 bytes")
        self._send_frame(0x9, payload)

    def close(self) -> None:
        sock = self._sock
        if sock is not None and not self._close_sent:
            try:
                self._send_frame(0x8, b"")
                self._close_sent = True
            except OSError:
                pass
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "WebSocketConnection":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
