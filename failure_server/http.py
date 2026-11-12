"""Dependency-free HTTP server and local control API."""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .capture import import_capture
from .playback import playback_response
from .replay import Config, Response, Run, SCENARIOS, text_response


def detect_lan_address():
    """Find the active interface address without sending traffic to an external server."""
    if sys.platform == 'darwin':
        try:
            route = subprocess.run(['/sbin/route', '-n', 'get', 'default'],
                                   capture_output=True, text=True, check=True, timeout=2)
            interface = re.search(r'interface:\s*(\S+)', route.stdout)
            if interface:
                result = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', interface[1]],
                                        capture_output=True, text=True, check=True, timeout=2)
                address = ipaddress.IPv4Address(result.stdout.strip())
                if not address.is_loopback and not address.is_unspecified:
                    return str(address)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # UDP connect only selects a route; no datagram is sent.
            probe.connect(('192.0.2.1', 9))
            address = ipaddress.IPv4Address(probe.getsockname()[0])
            if not address.is_loopback and not address.is_unspecified:
                return str(address)
    except (OSError, ValueError):
        pass
    raise ValueError('Could not detect a LAN address; set --public-base-url explicitly')


class ReplayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, catalog, data, public_base="", default_config=None):
        self.catalog, self.data = catalog, Path(data)
        self.public_base = public_base.rstrip("/")
        if self.public_base:
            parsed = urlsplit(self.public_base)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("public-base-url must be an HTTP(S) origin without a path or credentials")
        self.default_config = Config.from_dict(default_config or {}, catalog)
        self.runs, self.default_id = {}, None
        self.state_lock = threading.RLock()
        super().__init__(address, Handler)

    def create_run(self, values, default=False):
        config = Config.from_dict(values, self.catalog)
        run = Run(self.catalog, config, self.data)
        with self.state_lock:
            if len(self.runs) >= 100:
                _, oldest = next(iter(self.runs.items()))
                oldest.close()
                self.runs.pop(oldest.id)
            self.runs[run.id] = run
            if default:
                self.default_id = run.id
        return run

    def default_run(self):
        with self.state_lock:
            run = self.runs.get(self.default_id)
            return run if run else self.create_run(self.default_config.__dict__, default=True)

    def server_close(self):
        with self.state_lock:
            for run in self.runs.values():
                run.close()
        super().server_close()



def is_vast_path(path):
    path = path.rstrip('/')
    return path in ('/vast', '/vast.xml', '/request', '/vast/request') or path.endswith('/4.1/request')


class Handler(BaseHTTPRequestHandler):
    server_version = "FailureServer/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Captured/Charles-mapped URLs can contain credentials. Use the run's canonical event log.
        pass

    def handle(self):
        # Players routinely abandon keep-alive connections on seeks and playback stops.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def base(self):
        return self.server.public_base or f"http://{self.headers.get('Host', '127.0.0.1')}"

    def json_response(self, value, status=200):
        return text_response(json.dumps(value, allow_nan=False), "application/json", status)

    def payload(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length") from error
        if size < 0 or size > 65536:
            raise ValueError("Control payload must be at most 64 KiB")
        value = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    def read_ad_payload(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= 65536:
            raise ValueError('Ad request body must be at most 64 KiB')
        self.rfile.read(size)

    def do_OPTIONS(self):
        self.send(Response(status=204))

    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if is_vast_path(path):
                self.read_ad_payload()
                self.handle_request()
                return
            if path in ('/playback/play/v1/tuneSource', '/playback/play/v1/liveUpdate', '/tuneSource', '/liveUpdate'):
                payload = self.payload()
                run = self.server.default_run()
                endpoint = path.rsplit('/', 1)[-1]
                result = playback_response(run, self.base(), endpoint, payload)
                run.record(endpoint, 200)
                self.send(self.json_response(result))
                return
            if path in ("/api/runs", "/api/default"):
                run = self.server.create_run(self.payload(), default=path == "/api/default")
                self.send(self.json_response(run.summary(self.base(), current=path == "/api/default"), 201))
                return
            match = re.fullmatch(r"/api/runs/([a-f0-9]{12})/trigger", path)
            if match:
                with self.server.state_lock:
                    run = self.server.runs.get(match[1])
                if not run:
                    self.send(self.json_response({"error": "Run not found"}, 404))
                    return
                if run.config.scenario != "segment_load_exhausted":
                    raise ValueError("Only the active segment-load scenario has a trigger")
                run.arm.set()
                run.record("/trigger", 200, "manual-trigger")
                self.send(self.json_response(run.summary(self.base())))
                return
            self.send(self.json_response({"error": "Unknown control endpoint"}, 404))
        except (ValueError, TypeError, KeyError) as error:
            self.close_connection = True
            self.send(self.json_response({"error": str(error)}, 400))

    def do_DELETE(self):
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/api/runs/([a-f0-9]{12})", path)
        if not match:
            self.send(self.json_response({"error": "Unknown control endpoint"}, 404))
            return
        with self.server.state_lock:
            run = self.server.runs.pop(match[1], None)
        if run:
            run.close()
        self.send(Response(status=204 if run else 404))

    def handle_request(self, head=False):
        path = urlsplit(self.path).path
        run, resource = None, None
        current = False
        try:
            if path == "/":
                self.send(Response(Path(__file__).with_name("control.html").read_bytes(), "text/html; charset=utf-8"), head)
                return
            if path == "/health":
                self.send(self.json_response({"ok": True, "runs": len(self.server.runs)}), head)
                return
            if path == '/api/default':
                self.send(self.json_response(self.server.default_run().summary(self.base(), current=True)), head)
                return
            if path == "/api/scenarios":
                self.send(self.json_response(SCENARIOS), head)
                return
            if path == "/api/capture":
                c = self.server.catalog
                self.send(self.json_response({"capture": c["capture_name"], "capture_sha256": c["capture_sha256"],
                    "transactions": c["transactions"], "content_segments": len(c["content"]), "keys": len(c["keys"]),
                    "ads": [{"id": a["id"], "duration": a["duration"], "segments": len(a["segments"])} for a in c["ads"]],
                    "normalization": c["warnings"]}), head)
                return
            match = re.fullmatch(r"/api/runs/([a-f0-9]{12})", path)
            if match:
                with self.server.state_lock:
                    run = self.server.runs.get(match[1])
                self.send(self.json_response(run.summary(self.base(), current=run.id == self.server.default_id) if run else {"error": "Run not found"},
                                             200 if run else 404), head)
                return
            match = re.fullmatch(r"/runs/([a-f0-9]{12})/(.+)", path)
            if match:
                with self.server.state_lock:
                    run = self.server.runs.get(match[1])
                resource = match[2]
            elif path.startswith('/current/'):
                run, resource = self.server.default_run(), path[len('/current/'):]
                current = True
            elif path in ("/master.m3u8", "/live.m3u8", "/vast.xml", "/asset-list.json"):
                run, resource = self.server.default_run(), path[1:]
                current = True
            elif path.endswith("/mv_long.m3u8"):
                run, resource = self.server.default_run(), "master.m3u8"
                current = True
            elif path.endswith("_slide_long.m3u8"):
                run, resource = self.server.default_run(), "live.m3u8"
                current = True
            elif is_vast_path(path):
                run, resource = self.server.default_run(), "vast.xml"
                current = True
            if not run or not resource:
                self.send(self.json_response({"error": "Unknown replay resource"}, 404), head)
                return
            response = self.resource(run, resource, head, current=current)
            run.record(resource, response.status if not response.disconnect else 0,
                       'injected-' + response.headers['X-Replay-Fault'] if 'X-Replay-Fault' in response.headers
                       else "disconnect" if response.disconnect else "response")
            self.send(response, head)
        except (ValueError, IndexError, FileNotFoundError):
            self.send(self.json_response({"error": "Replay resource not found"}, 404), head)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def resource(self, run, resource, head, current=False):
        base = self.base()
        if resource == "master.m3u8":
            return run.master(base, current=current)
        if resource == "live.m3u8":
            return run.playlist(base, current=current)
        if resource == "vast.xml":
            return run.vast(base, current=current)
        if resource == "asset-list.json":
            return run.asset_list(base, current=current)
        if resource.startswith("beacon/"):
            return Response(status=204)
        match = re.fullmatch(r"keys/(\d+)\.bin", resource)
        if match:
            return Response((run.data / run.catalog["keys"][int(match[1])]).read_bytes())
        match = re.fullmatch(r"content/(\d+)\.aac", resource)
        if match and int(match[1]) < 100000:
            return Response((run.data / run.content(int(match[1]))["file"]).read_bytes(), "audio/aac")
        match = re.fullmatch(r"ads/(\d+)/(master|media)\.m3u8", resource)
        if match:
            index = int(match[1])
            if index >= len(run.ads):
                raise IndexError
            return run.ad_playlist(base, index, match[2] == "master", current=current)
        match = re.fullmatch(r"ads/(\d+)/segments/(\d+)\.aac", resource)
        if match:
            index, segment = int(match[1]), int(match[2])
            if index >= len(run.ads) or segment >= len(run.ads[index]["segments"]):
                raise IndexError
            return run.segment(index, segment, head=head)
        raise ValueError("Unknown replay resource")

    def send(self, response, head=False):
        if response.disconnect:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        body, status, headers = response.body, response.status, dict(response.headers)
        range_header = self.headers.get("Range")
        if range_header and status == 200 and response.content_type in ("audio/aac", "application/octet-stream"):
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            length = len(body)
            if not match or not any(match.groups()) or not length:
                status, body = 416, b""
                headers["Content-Range"] = f"bytes */{length}"
            else:
                left, right = match.groups()
                start = int(left) if left else max(0, length - int(right))
                end = min(length - 1, int(right)) if left and right else length - 1
                if start > end or start >= length:
                    status, body = 416, b""
                    headers["Content-Range"] = f"bytes */{length}"
                else:
                    status, body = 206, body[start:end + 1]
                    headers["Content-Range"] = f"bytes {start}-{end}/{length}"
        self.send_response(status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Replay captured HLS ads with configurable failures")
    parser.add_argument("--capture", type=Path, default=Path("captures/fulladbreak.chlz"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-base-url", default="", help="Override the automatically detected LAN replay origin")
    parser.add_argument("--scenario", choices=SCENARIOS, default="healthy")
    parser.add_argument("--mode", choices=("sxm", "standard"), default="sxm")
    parser.add_argument("--ad-count", type=int, default=2)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--import-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.capture.exists():
            catalog = import_capture(args.capture, args.data)
        else:
            catalog = json.loads((args.data / "catalog.json").read_text())
        if args.import_only:
            print(f"Imported {len(catalog['ads'])} ads, {len(catalog['content'])} content segments, {len(catalog['keys'])} AES keys")
            return
        if bool(args.tls_cert) != bool(args.tls_key):
            raise ValueError("Provide both --tls-cert and --tls-key")
        scheme = 'https' if args.tls_cert else 'http'
        public_host = None
        if not args.public_base_url:
            public_host = detect_lan_address() if args.host == '0.0.0.0' else args.host
        server = ReplayServer((args.host, args.port), catalog, args.data, args.public_base_url,
                              {"scenario": args.scenario, "mode": args.mode, "ad_count": args.ad_count})
        if public_host:
            server.public_base = f'{scheme}://{public_host}:{server.server_port}'
        if args.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    scheme = 'https' if args.tls_cert else 'http'
    local_host = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
    local_base = f'{scheme}://{local_host}:{server.server_port}'
    print(f"Control: {local_base}/", flush=True)
    print(f"Charles Map Remote destination: {local_base}", flush=True)
    for endpoint in ('tuneSource', 'liveUpdate'):
        path = f'/playback/play/v1/{endpoint}'
        print(f"  https://api.edge-gateway.siriusxm.com{path} -> {local_base}{path}", flush=True)
    print("  https://siriusxmdigital.deliveryengine.adswizz.com/vast/4.1/request"
          f" -> {local_base}/vast.xml", flush=True)
    print(f"Phone replay origin: {server.public_base}", flush=True)
    print(f"Alternative to AdsWizz mapping — Tuner ads_sdk_config.forceTestUrl: {server.public_base}/vast.xml (isForceTestEnabled=true)", flush=True)
    print(f"Replay: {len(catalog['ads'])} captured ads; default pod uses {args.ad_count}. Scenario: {args.scenario}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
