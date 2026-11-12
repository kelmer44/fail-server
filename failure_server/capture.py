"""Import Charles .chlz recordings without retaining request headers or tracking URLs."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

from .playback import save_playback_fixture

ATTRS = re.compile(r'([A-Z0-9-]+)=("[^"\n]*"|[^,]*)')
SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000,
                22050, 16000, 12000, 11025, 8000, 7350)


def attributes(line: str) -> dict[str, str]:
    return {key: value.strip('"') for key, value in ATTRS.findall(line)}


def url_key(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower(), unquote(parsed.path)


@dataclass
class Transaction:
    number: int
    url: str
    body: bytes
    mime: str
    timestamp: str | None = None


def read_capture(path: Path) -> list[Transaction]:
    transactions = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for name in sorted((n for n in names if n.endswith("-meta.json")),
                           key=lambda n: int(n.split("-", 1)[0])):
            meta = json.loads(archive.read(name))
            response = meta.get("response") or {}
            body_file = response.get("bodyFile")
            if meta.get("method") == "CONNECT" or response.get("status") != 200 or body_file not in names:
                continue
            body = archive.read(body_file)
            if body.startswith(b"\x1f\x8b"):
                body = gzip.decompress(body)
            host = meta.get("host", "")
            scheme = meta.get("scheme") or "https"
            port = meta.get("port")
            authority = host if not port or port in (80, 443) else f"{host}:{port}"
            url = f"{scheme}://{authority}{meta.get('path') or '/'}"
            transactions.append(Transaction(int(name.split("-", 1)[0]), url, body,
                                            response.get("mimeType") or "application/octet-stream",
                                            (meta.get("times") or {}).get("start")))
    return transactions


def parse_playlist(text: str, base_url: str) -> list[dict]:
    """Preserve original IVs even when replay changes the segment sequence/timestamps."""
    result, key, duration, timestamp = [], None, None, None
    sequence = 0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            timestamp = datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00"))
        elif line.startswith("#EXT-X-KEY:"):
            value = attributes(line)
            if value.get("METHOD") == "NONE":
                key = None
            elif value.get("METHOD") == "AES-128":
                key = {"url": urljoin(base_url, value["URI"]), "iv": value.get("IV")}
            else:
                raise ValueError("Only AES-128 and unencrypted captured media are supported")
        elif line.startswith("#EXT-X-BYTERANGE:"):
            raise ValueError("Byte-range playlists require a separate importer")
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            segment = {"url": urljoin(base_url, line), "duration": duration, "sequence": sequence}
            if key:
                segment["key_url"] = key["url"]
                segment["iv"] = key["iv"] or f"0x{sequence:032x}"
            if timestamp:
                segment["timestamp"] = timestamp.isoformat()
                timestamp += timedelta(seconds=duration)
            result.append(segment)
            sequence += 1
            duration = None
    return result


def split_adts(body: bytes, chunk_seconds: float = 2.0) -> list[tuple[bytes, float]]:
    """Split recorded AAC only at ADTS frame boundaries; strip old ID3 timestamps."""
    chunks, frames = [], []
    elapsed, position = 0.0, 0
    while position < len(body):
        if body[position:position + 3] == b"ID3":
            if position + 10 > len(body):
                raise ValueError("Truncated ID3 header")
            size_bytes = body[position + 6:position + 10]
            if any(value & 0x80 for value in size_bytes):
                raise ValueError("Invalid ID3 size")
            size = sum(value << shift for value, shift in zip(size_bytes, (21, 14, 7, 0)))
            position += 10 + size + (10 if body[position + 5] & 0x10 else 0)
            if position > len(body):
                raise ValueError("Truncated ID3 tag")
            continue
        if position + 7 > len(body) or body[position] != 0xFF or body[position + 1] & 0xF6 != 0xF0:
            raise ValueError(f"Expected an ADTS frame at byte {position}")
        header = body[position:position + 7]
        rate_index = (header[2] >> 2) & 0x0F
        length = ((header[3] & 3) << 11) | (header[4] << 3) | (header[5] >> 5)
        if rate_index >= len(SAMPLE_RATES) or length < 7 or position + length > len(body):
            raise ValueError("Invalid ADTS frame")
        frames.append(body[position:position + length])
        elapsed += 1024 * (1 + (header[6] & 3)) / SAMPLE_RATES[rate_index]
        position += length
        if elapsed >= chunk_seconds:
            chunks.append((b"".join(frames), elapsed))
            frames, elapsed = [], 0.0
    if frames:
        chunks.append((b"".join(frames), elapsed))
    if not chunks:
        raise ValueError("No AAC frames in captured ad")
    return chunks


def _variants(transaction: Transaction, by_url: dict) -> list[Transaction]:
    text = transaction.body.decode("utf-8-sig")
    if "#EXTINF:" in text:
        return [transaction]
    result = []
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            candidate = by_url.get(url_key(urljoin(transaction.url, line.strip())))
            if candidate and b"#EXTINF:" in candidate.body:
                result.append(candidate)
    return result


def import_capture(source: Path, output: Path) -> dict:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog_path = output / "catalog.json"
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text())
        if catalog.get("capture_sha256") != digest or catalog.get("format") != 1:
            raise ValueError("Data directory belongs to another capture; choose a different --data directory")
        if not (output / 'playback.json').exists() or json.loads((output / 'playback.json').read_text()).get('format') != 2:
            save_playback_fixture(read_capture(source), output)
        return catalog
    output.mkdir(parents=True, exist_ok=True)
    transactions = read_capture(source)
    save_playback_fixture(transactions, output)
    by_url = {url_key(t.url): t for t in transactions}
    vast = None
    for transaction in transactions:
        if b"<VAST" in transaction.body[:500]:
            candidate = ET.fromstring(transaction.body)
            for element in candidate.iter():
                element.tag = element.tag.split("}")[-1]
            if candidate.findall("Ad"):
                vast = candidate
                break
    if vast is None:
        raise ValueError("No successful VAST ad response found in capture")

    def save(relative: str, data: bytes) -> str:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return relative

    ads, warnings = [], []
    for ad in sorted(vast.findall("Ad"), key=lambda value: int(value.get("sequence", "0"))):
        media = ad.findall(".//Linear/MediaFiles/MediaFile")
        choices = []
        for element in media:
            reference = (element.text or "").strip()
            master = by_url.get(url_key(reference))
            if not master and not urlsplit(reference).hostname:
                matches = [t for (host, path), t in by_url.items() if path == url_key(reference)[1]]
                if len(matches) == 1:
                    master = matches[0]
            if not master:
                continue
            for variant in _variants(master, by_url):
                segments = parse_playlist(variant.body.decode("utf-8-sig"), variant.url)
                if segments and all(url_key(s["url"]) in by_url for s in segments):
                    quality = int(re.search(r"/(\d+)k/", variant.url).group(1)) if re.search(r"/(\d+)k/", variant.url) else 0
                    choices.append((quality, segments))
        if not choices:
            raise ValueError(f"Ad sequence {ad.get('sequence')} has no completely captured media rendition")
        _, captured_segments = max(choices, key=lambda value: value[0])
        if any("key_url" in segment for segment in captured_segments):
            raise ValueError("Encrypted ad resegmentation is not supported")
        chunks = []
        for segment in captured_segments:
            chunks.extend(split_adts(by_url[url_key(segment["url"])].body))
        index = len(ads)
        result = []
        for number, (data, duration) in enumerate(chunks):
            result.append({"file": save(f"media/ad-{index}-{number}.aac", data), "duration": duration})
        ads.append({"id": f"ad-{index + 1}", "segments": result,
                    "media_attributes": dict(media[0].attrib),
                    "duration": sum(s["duration"] for s in result)})

    candidates = []
    for transaction in transactions:
        if b"#EXT-X-DATERANGE:" not in transaction.body[:300000] or b"#EXTINF:" not in transaction.body[:300000]:
            continue
        text = transaction.body.decode("utf-8-sig")
        segments = parse_playlist(text, transaction.url)
        captured = [s for s in segments if url_key(s["url"]) in by_url]
        if captured:
            candidates.append((len(captured), transaction.number, captured))
    if not candidates:
        raise ValueError("No captured primary-content segments referenced by an interstitial playlist")
    _, _, selected = max(candidates, key=lambda value: value[:2])
    keys, content = {}, []
    for number, segment in enumerate(selected):
        entry = {"file": save(f"media/content-{number}.aac", by_url[url_key(segment["url"])].body),
                 "duration": segment["duration"]}
        if "key_url" in segment:
            key_url = segment["key_url"]
            if key_url not in keys:
                transaction = by_url.get(url_key(key_url))
                if not transaction:
                    raise ValueError("A primary-content encryption key is missing from the recording")
                raw = transaction.body
                if len(raw) != 16:
                    raw = base64.b64decode(json.loads(raw)["key"], validate=True)
                if len(raw) != 16:
                    raise ValueError("Expected a 16-byte AES-128 key")
                key_id = str(len(keys))
                keys[key_url] = key_id
                save(f"keys/{key_id}.bin", raw)
            entry.update(key=keys[key_url], iv=segment["iv"])
        content.append(entry)
    warnings.append("Primary content is rebuilt from captured segments, with discontinuities across segment boundaries.")
    warnings.append("Ad AAC is resegmented at ADTS frame boundaries to make later-segment failures possible.")
    catalog = {"format": 1, "capture_sha256": digest, "capture_name": source.name,
               "transactions": len(transactions), "ads": ads, "content": content,
               "keys": [f"keys/{i}.bin" for i in range(len(keys))], "warnings": warnings}
    temporary = catalog_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, indent=2) + "\n")
    temporary.replace(catalog_path)
    return catalog
