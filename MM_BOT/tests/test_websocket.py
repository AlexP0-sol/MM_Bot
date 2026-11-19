import base64
import hashlib
import socket
import struct
import threading
import unittest

from gridbot.websocket import WebSocketConfig, WebSocketConnection, WebSocketError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bool = False) -> bytes:
    first = (0x80 if fin else 0) | opcode
    length = len(payload)
    if length < 126:
        header = bytes([first, (0x80 if mask else 0) | length])
    elif length <= 0xFFFF:
        header = bytes([first, (0x80 if mask else 0) | 126]) + struct.pack("!H", length)
    else:
        header = bytes([first, (0x80 if mask else 0) | 127]) + struct.pack("!Q", length)
    if not mask:
        return header + payload
    key = b"\x01\x02\x03\x04"
    return header + key + bytes(value ^ key[index % 4] for index, value in enumerate(payload))


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = sock.recv(2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", sock.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", sock.recv(8))[0]
    key = sock.recv(4) if masked else b""
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if masked:
        payload = bytes(value ^ key[index % 4] for index, value in enumerate(payload))
    return opcode, payload


class FakeServer:
    def __init__(self, behavior):
        self.behavior = behavior
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            connection, _ = self.socket.accept()
            connection.settimeout(2)
            with connection:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(1)
                    if not chunk:
                        return
                    request += chunk
                key = None
                for line in request.decode("iso-8859-1").split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                if key is None:
                    return
                accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
                self.behavior(connection, accept)
        except Exception as exc:  # surfaced by the test thread
            self.error = exc
        finally:
            self.socket.close()

    def join(self):
        self.thread.join(timeout=3)
        if self.error:
            raise self.error


def send_handshake(connection, accept):
    connection.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii")
    )


class WebSocketTests(unittest.TestCase):
    def connect(self, behavior):
        server = FakeServer(behavior)
        connection = WebSocketConnection(
            f"ws://127.0.0.1:{server.port}/stream",
            config=WebSocketConfig(allow_insecure_ws=True, connect_timeout_s=2, recv_timeout_s=2),
        )
        connection.connect()
        return connection, server

    def test_handshake_and_text_message(self):
        def behavior(connection, accept):
            send_handshake(connection, accept)
            connection.sendall(_frame(0x1, b"hello"))

        connection, server = self.connect(behavior)
        try:
            self.assertEqual(connection.recv_text(), "hello")
        finally:
            connection.close()
            server.join()

    def test_ping_is_answered_and_fragments_are_joined(self):
        def behavior(connection, accept):
            send_handshake(connection, accept)
            connection.sendall(_frame(0x9, b"x"))
            opcode, payload = _read_frame(connection)
            assert opcode == 0xA and payload == b"x"
            connection.sendall(_frame(0x1, b"hel", fin=False))
            connection.sendall(_frame(0x0, b"lo", fin=True))

        connection, server = self.connect(behavior)
        try:
            self.assertEqual(connection.recv_text(), "hello")
        finally:
            connection.close()
            server.join()

    def test_bad_accept_key_is_rejected(self):
        def behavior(connection, accept):
            send_handshake(connection, "wrong")

        server = FakeServer(behavior)
        connection = WebSocketConnection(
            f"ws://127.0.0.1:{server.port}/stream",
            config=WebSocketConfig(allow_insecure_ws=True, connect_timeout_s=2),
        )
        with self.assertRaises(WebSocketError):
            connection.connect()
        server.join()

    def test_insecure_ws_is_refused_outside_loopback(self):
        connection = WebSocketConnection(
            "ws://example.com/stream", config=WebSocketConfig(allow_insecure_ws=True)
        )
        with self.assertRaises(WebSocketError):
            connection.connect()

    def test_masked_server_frame_fails_closed(self):
        def behavior(connection, accept):
            send_handshake(connection, accept)
            connection.sendall(_frame(0x1, b"hello", mask=True))

        connection, server = self.connect(behavior)
        try:
            with self.assertRaises(WebSocketError):
                connection.recv_text()
        finally:
            connection.close()
            server.join()


if __name__ == "__main__":
    unittest.main()
