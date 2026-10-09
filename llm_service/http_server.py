"""Ограниченный HTTP-шлюз; заголовки и содержимое чата не журналируются."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import socket
import threading
import time
import uuid

from .policy import MAX_BODY, MODEL, ServiceError

LOG = logging.getLogger("tabletop.llm_service")
READ_TIMEOUT = 10


class ServiceServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, policy, backend):
        self.policy = policy
        self.backend = backend
        self._handlers = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self._handlers.acquire(blocking=False):
            try:
                request.settimeout(1)
                body = json.dumps(ServiceError(503, "server_busy", "Сервис занят.").body()).encode()
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                b"Retry-After: 1\r\nConnection: close\r\nContent-Length: "
                                + str(len(body)).encode() + b"\r\n\r\n" + body)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handlers.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handlers.release()

    def handle_error(self, request, client_address):
        LOG.error("Ошибка HTTP-обработчика; соединение закрыто.")


class Handler(BaseHTTPRequestHandler):
    server_version = "TabletopLLM"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)
        self._started = time.monotonic()
        self._request_id = uuid.uuid4().hex[:12]
        self._read_expired = False
        self._read_timer = threading.Timer(READ_TIMEOUT, self._expire_read)
        self._read_timer.daemon = True
        self._read_timer.start()

    def _expire_read(self):
        self._read_expired = True
        try:
            # Завершает и медленное чтение заголовков, и чтение тела; запись ответа доступна.
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def handle(self):
        try:
            super().handle()
        finally:
            self._read_timer.cancel()

    def log_message(self, *args):
        # Стандартный журнал включает URL; клиент мог поместить в него секрет.
        pass

    def _send(self, status, body, retry_after=None):
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.send_header("X-Request-ID", self._request_id)
            if retry_after is not None:
                self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(encoded)
        except OSError:
            pass
        finally:
            self.close_connection = True
            LOG.info("Запрос %s: HTTP %d, %.3f с", self._request_id, status, time.monotonic() - self._started)

    def _error(self, error):
        self._send(error.status, error.body(), error.retry_after)

    def send_error(self, code, message=None, explain=None):
        self._error(ServiceError(code, "http_error", "Неподдерживаемый HTTP-запрос."))

    def _authorize(self):
        headers = self.headers.get_all("Authorization", [])
        self.server.policy.authorize(headers[0] if len(headers) == 1 else None)

    def do_GET(self):
        self._read_timer.cancel()
        try:
            if self.path == "/health":
                ready = self.server.backend.ready()
                self._send(200 if ready else 503, {"status": "ok" if ready else "unavailable"})
            elif self.path == "/v1/models":
                self._authorize()
                self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "local"}]})
            else:
                raise ServiceError(404, "not_found", "Маршрут не найден.")
        except ServiceError as error:
            self._error(error)

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or "Transfer-Encoding" in self.headers):
            raise ServiceError(400, "invalid_body", "Нужна корректная длина JSON-тела.")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise ServiceError(413, "body_too_large", "Размер тела превышает 128 КиБ.")
        if self.headers.get_content_type() != "application/json":
            raise ServiceError(400, "invalid_body", "Нужен Content-Type: application/json.")
        try:
            body = self.rfile.read(length)
            if self._read_expired:
                raise socket.timeout("read deadline exceeded")
            if len(body) != length:
                raise ValueError("incomplete body")

            def reject_constant(value):
                raise ValueError("non-finite JSON")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate JSON key")
                    result[key] = value
                return result

            return json.loads(body.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object)
        except socket.timeout as exc:
            raise ServiceError(408, "request_timeout", "Истекло время чтения запроса.") from exc
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ServiceError(400, "invalid_body", "Некорректное JSON-тело.") from exc
        finally:
            # Ограничение относится к вводу HTTP, а не к долгой генерации.
            self._read_timer.cancel()

    def do_POST(self):
        try:
            if self.path != "/v1/chat/completions":
                raise ServiceError(404, "not_found", "Маршрут не найден.")
            self._authorize()
            payload = self.server.policy.validate(self._body())
            self.server.policy.admit()
            with self.server.policy.slot():
                answer = self.server.backend.complete(payload, self.server.policy)
            self._send(200, answer)
        except ServiceError as error:
            self._error(error)
        except Exception:
            self._error(ServiceError(500, "internal_error", "Ошибка обработки запроса."))
