"""Per-run replay state, playlist generation, and failure decisions."""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

SCENARIOS = {
    "healthy": "Recorded ads and content play without injected errors",
    "asset_list_error": "Fail both VAST requests and standard HLS asset-list requests",
    "first_prepare_error": "Return an invalid playlist for the first ad",
    "last_prepare_error": "Return an invalid playlist for the last selected ad",
    "all_prepare_error": "Return invalid playlists for every selected ad",
    "segment_load_exhausted": "Hold a later first-ad segment; trigger persistent load failures after playback starts",
}


@dataclass(frozen=True)
class Config:
    scenario: str = "healthy"
    mode: str = "sxm"
    ad_count: int = 2
    pre_roll_segments: int = 1
    window_segments: int = 0
    fault_ad_index: int = 0
    fault_segment_index: int = 3
    error_status: int = 503
    transport: str = "http"
    gate_timeout_seconds: float = 60.0
    timeout_seconds: float = 30.0
    failure_count: int | None = None
    start_armed: bool = False

    @classmethod
    def from_dict(cls, values: dict, catalog: dict) -> "Config":
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a JSON object")
        allowed = cls.__dataclass_fields__
        if set(values) - set(allowed):
            raise ValueError("Unknown configuration fields: " + ", ".join(sorted(set(values) - set(allowed))))
        config = cls(**values)
        if config.scenario not in SCENARIOS:
            raise ValueError("Unknown scenario; renderer failures require app-side injection")
        if config.mode not in ("sxm", "standard"):
            raise ValueError("mode must be sxm or standard")
        for name in ("ad_count", "pre_roll_segments", "window_segments", "fault_ad_index", "fault_segment_index", "error_status"):
            if type(getattr(config, name)) is not int:
                raise ValueError(f"{name} must be an integer")
        if not 1 <= config.ad_count <= len(catalog["ads"]):
            raise ValueError("ad_count exceeds the captured pod")
        if not 1 <= config.pre_roll_segments <= 30 or not 0 <= config.window_segments <= 1000:
            raise ValueError("Invalid pre-roll or live window size")
        if not 0 <= config.fault_ad_index < config.ad_count or config.fault_segment_index < 1:
            raise ValueError("Fault must target a later segment of a selected ad")
        if config.fault_segment_index >= len(catalog["ads"][config.fault_ad_index]["segments"]):
            raise ValueError("Selected ad has no segment at fault_segment_index")
        if not 400 <= config.error_status <= 599 or config.transport not in ("http", "disconnect", "timeout"):
            raise ValueError("Invalid error status or transport")
        for name in ("gate_timeout_seconds", "timeout_seconds"):
            value = getattr(config, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 300:
                raise ValueError(f"{name} must be between 0 and 300 seconds")
        if config.failure_count is not None and (type(config.failure_count) is not int or config.failure_count < 1):
            raise ValueError("failure_count must be null or a positive integer")
        if type(config.start_armed) is not bool:
            raise ValueError("start_armed must be a boolean")
        return config


@dataclass
class Response:
    body: bytes = b""
    content_type: str = "application/octet-stream"
    status: int = 200
    headers: dict = field(default_factory=dict)
    disconnect: bool = False


def text_response(text: str, content_type="application/vnd.apple.mpegurl", status=200) -> Response:
    return Response(text.encode(), content_type, status)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Run:
    def __init__(self, catalog: dict, config: Config, data: Path, clock=time.monotonic):
        self.id = uuid.uuid4().hex[:12]
        self.catalog, self.config, self.data, self.clock = catalog, config, data, clock
        self.ads = catalog["ads"][:config.ad_count]
        self.lock = threading.RLock()
        self.arm = threading.Event()
        self.closed = threading.Event()
        if config.start_armed:
            self.arm.set()
        self.created = clock()
        self.requests = []
        self.counts = {}
        self.failed_segments = 0
        self.waiting_segments = 0
        self.boundaries = [0.0]
        self.break_start = sum(self.content(i)["duration"] for i in range(config.pre_roll_segments))
        self.break_duration = sum(ad["duration"] for ad in self.ads)
        required = self.break_start + self.break_duration + 20
        while self.boundaries[-1] < required:
            self.boundaries.append(self.boundaries[-1] + self.content(len(self.boundaries) - 1)["duration"])
        self.initial_duration = self.boundaries[-1]
        self.epoch = datetime.now(timezone.utc) - timedelta(seconds=self.initial_duration)

    def content(self, index: int) -> dict:
        return self.catalog["content"][index % len(self.catalog["content"])]

    def url(self, base: str, path: str) -> str:
        return f"{base.rstrip('/')}/runs/{self.id}/{path}"

    def current_url(self, base: str, path: str) -> str:
        return f"{base.rstrip('/')}/current/{path}"

    def record(self, path: str, status: int, event: str = "response") -> None:
        with self.lock:
            self.counts[path] = self.counts.get(path, 0) + 1
            self.requests.append({"elapsed": round(self.clock() - self.created, 3), "path": path,
                                  "status": status, "event": event})
            self.requests = self.requests[-250:]

    def summary(self, base: str, current: bool = False) -> dict:
        url = self.current_url if current else self.url
        with self.lock:
            return {"id": self.id, "config": asdict(self.config), "armed": self.arm.is_set(),
                    "waiting_segments": self.waiting_segments, "injected_segment_failures": self.failed_segments,
                    "break_start_seconds": self.break_start, "break_duration_seconds": self.break_duration,
                    "requests": list(self.requests), "counts": dict(self.counts),
                    "urls": {"master": url(base, "master.m3u8"),
                             "vast": url(base, "vast.xml"),
                             "asset_list": url(base, "asset-list.json"),
                             "status": f"{base}/api/runs/{self.id}",
                             "trigger": f"{base}/api/runs/{self.id}/trigger"}}

    def master(self, base: str, current: bool = False) -> Response:
        url = self.current_url if current else self.url
        return text_response('#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-STREAM-INF:BANDWIDTH=256000,CODECS="mp4a.40.2"\n'
                             + url(base, "live.m3u8") + "\n")

    def playlist(self, base: str, current: bool = False) -> Response:
        url = self.current_url if current else self.url
        available = self.initial_duration + max(0, self.clock() - self.created)
        with self.lock:
            while self.boundaries[-1] <= available and len(self.boundaries) < 10000:
                self.boundaries.append(self.boundaries[-1] + self.content(len(self.boundaries) - 1)["duration"])
            count = len(self.boundaries) - 1
            if self.boundaries[-1] > available:
                count -= 1
            count = max(1, count)
            start = max(0, count - self.config.window_segments) if self.config.window_segments else 0
            positions = self.boundaries[start:count]
        target = math.ceil(max(s["duration"] for s in self.catalog["content"]))
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{start}", f"#EXT-X-DISCONTINUITY-SEQUENCE:{start}", '#EXT-X-START:TIME-OFFSET=0,PRECISE=YES']
        if not self.config.window_segments:
            lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
        cue = self.epoch + timedelta(seconds=self.break_start)
        end = cue + timedelta(seconds=self.break_duration)
        asset_list = (f"sxmp://interstitial/req?_HLS_interstitial_id=replay-break&_pod_duration={self.break_duration:.9f}"
                      if self.config.mode == "sxm" else url(base, "asset-list.json"))
        lines.extend([
            f'#EXT-X-DATERANGE:ID="prefetch_replay-break",START-DATE="{stamp(cue - timedelta(seconds=4))}",'
            f'END-DATE="{stamp(cue)}",X-PREFETCH-DURATION="{self.break_duration:.9f}",X-PREFETCH-ID="replay-break"',
            f'#EXT-X-DATERANGE:ID="replay-break",CLASS="com.apple.hls.interstitial",START-DATE="{stamp(cue)}",'
            f'PLANNED-DURATION={self.break_duration:.9f},X-ASSET-LIST="{asset_list}",X-SNAP="OUT,IN",'
            'X-TIMELINE-OCCUPIES="RANGE",X-TIMELINE-STYLE="HIGHLIGHT",X-CONTENT-MAY-VARY="YES"',
            f'#EXT-X-DATERANGE:ID="replay-break",CLASS="com.apple.hls.interstitial",START-DATE="{stamp(cue)}",'
            f'END-DATE="{stamp(end)}",X-PLAYOUT-LIMIT={self.break_duration:.9f},X-RESUME-OFFSET={self.break_duration:.9f}',
        ])
        for index, position in zip(range(start, count), positions):
            segment = self.content(index)
            if index > start:
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f'#EXT-X-PROGRAM-DATE-TIME:{stamp(self.epoch + timedelta(seconds=position))}')
            if "key" in segment:
                lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="{url(base, "keys/" + segment["key"] + ".bin")}",IV={segment["iv"]}')
            else:
                lines.append("#EXT-X-KEY:METHOD=NONE")
            lines.extend([f'#EXTINF:{segment["duration"]:.9f},', url(base, f"content/{index}.aac")])
        return text_response("\n".join(lines) + "\n")

    def asset_list(self, base: str, current: bool = False) -> Response:
        url = self.current_url if current else self.url
        if self.config.scenario == "asset_list_error":
            return text_response('{"error":"injected asset-list failure"}', "application/json", self.config.error_status)
        return text_response(json.dumps({"ASSETS": [
            {"URI": url(base, f"ads/{i}/master.m3u8"), "DURATION": ad["duration"]}
            for i, ad in enumerate(self.ads)]}), "application/json")

    def vast(self, base: str, current: bool = False) -> Response:
        url = self.current_url if current else self.url
        if self.config.scenario == "asset_list_error":
            return text_response("Injected ad-request failure", "text/plain", self.config.error_status)
        root = ET.Element("VAST", version="4.2")
        for index, asset in enumerate(self.ads):
            ad = ET.SubElement(root, "Ad", id=asset["id"], sequence=str(index + 1), adType="audio")
            inline = ET.SubElement(ad, "InLine")
            ET.SubElement(inline, "AdSystem").text = "failure-server"
            ET.SubElement(inline, "AdTitle").text = asset["id"]
            ET.SubElement(inline, "Impression").text = url(base, f"beacon/{index}/impression")
            ET.SubElement(inline, "Error").text = url(base, f"beacon/{index}/error") + "?code=[ERRORCODE]"
            creative = ET.SubElement(ET.SubElement(inline, "Creatives"), "Creative", id=asset["id"])
            ET.SubElement(creative, "UniversalAdId", idRegistry="unknown").text = asset["id"]
            linear = ET.SubElement(creative, "Linear")
            seconds = asset["duration"]
            ET.SubElement(linear, "Duration").text = f"{int(seconds // 3600):02}:{int(seconds // 60) % 60:02}:{seconds % 60:06.3f}"
            tracking = ET.SubElement(linear, "TrackingEvents")
            for event in ("start", "firstQuartile", "midpoint", "thirdQuartile", "complete"):
                ET.SubElement(tracking, "Tracking", event=event).text = url(base, f"beacon/{index}/{event}")
            attributes = dict(asset.get("media_attributes") or {"delivery": "streaming", "type": "application/vnd.apple.mpegurl"})
            ET.SubElement(ET.SubElement(linear, "MediaFiles"), "MediaFile", attributes).text = url(base, f"ads/{index}/master.m3u8")
        return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True), "application/xml")

    def prepare_fails(self, index: int) -> bool:
        return (self.config.scenario == "all_prepare_error"
                or self.config.scenario == "first_prepare_error" and index == 0
                or self.config.scenario == "last_prepare_error" and index == len(self.ads) - 1)

    def ad_playlist(self, base: str, index: int, master: bool, current: bool = False) -> Response:
        url = self.current_url if current else self.url
        if self.prepare_fails(index):
            return text_response("injected invalid HLS playlist\n")
        if master:
            return text_response('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=256000,CODECS="mp4a.40.2"\n'
                                 + url(base, f"ads/{index}/media.m3u8") + "\n")
        segments = self.ads[index]["segments"]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-MEDIA-SEQUENCE:0",
                 f'#EXT-X-TARGETDURATION:{math.ceil(max(s["duration"] for s in segments))}']
        for number, segment in enumerate(segments):
            lines.extend([f'#EXTINF:{segment["duration"]:.9f},', url(base, f"ads/{index}/segments/{number}.aac")])
        return text_response("\n".join(lines + ["#EXT-X-ENDLIST", ""]))

    def segment(self, ad_index: int, segment_index: int, head: bool = False) -> Response:
        if not head and self.config.scenario == "segment_load_exhausted" and ad_index == self.config.fault_ad_index and segment_index >= self.config.fault_segment_index:
            with self.lock:
                self.waiting_segments += 1
            try:
                released = self.arm.wait(self.config.gate_timeout_seconds)
            finally:
                with self.lock:
                    self.waiting_segments -= 1
            if self.closed.is_set():
                return text_response("Replay stopped", "text/plain", 410)
            if not released:
                return text_response("Trigger was not released before gate timeout", "text/plain", 408)
            with self.lock:
                fail = self.config.failure_count is None or self.failed_segments < self.config.failure_count
                if fail:
                    self.failed_segments += 1
            if fail:
                if self.config.transport == "timeout":
                    self.closed.wait(self.config.timeout_seconds)
                if self.config.transport in ("disconnect", "timeout"):
                    return Response(disconnect=True)
                return text_response("Injected active-ad segment failure", "text/plain", self.config.error_status)
        segment = self.ads[ad_index]["segments"][segment_index]
        return Response((self.data / segment["file"]).read_bytes(), "audio/aac")

    def close(self) -> None:
        self.closed.set()
        self.arm.set()
