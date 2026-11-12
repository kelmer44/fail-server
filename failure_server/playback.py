"""Captured playback API fixtures rebased to a replay run's timeline."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .replay import stamp


def save_playback_fixture(transactions, output: Path):
    responses = {}
    updates = []
    tune_time = None
    for transaction in transactions:
        endpoint = urlsplit(transaction.url).path.rsplit('/', 1)[-1]
        if endpoint in ('tuneSource', 'liveUpdate') and endpoint not in responses:
            responses[endpoint] = json.loads(transaction.body)
            if endpoint == 'tuneSource':
                tune_time = transaction.timestamp
        if endpoint == 'liveUpdate':
            updates.append((transaction.timestamp, json.loads(transaction.body)))
    if 'tuneSource' not in responses:
        return
    tune = responses['tuneSource']
    anchor = datetime.fromisoformat(tune_time) if tune_time else None
    responses['format'] = 2
    responses['liveUpdates'] = [
        {'offset_seconds': max(0, (datetime.fromisoformat(timestamp) - anchor).total_seconds()) if timestamp and anchor else 0,
         'response': response}
        for timestamp, response in updates
    ]
    responses['liveUpdates'].sort(key=lambda entry: entry['offset_seconds'])
    # Never retain the original identity-bearing streaming URLs in extracted fixtures.
    for stream in tune.get('streams', []):
        for url in stream.get('urls', []):
            url['url'] = 'REPLAY_MASTER'
            url['encryptionKeyId'] = None
    (output / 'playback.json').write_text(json.dumps(responses, indent=2) + '\n')


def playback_response(run, base: str, endpoint: str, request: dict) -> dict:
    fixture = run.data / 'playback.json'
    if not fixture.exists():
        raise ValueError('Playback API fixtures are unavailable; import a capture containing tuneSource')
    responses = json.loads(fixture.read_text())
    tune = responses['tuneSource']
    requested_id = request.get('id' if endpoint == 'tuneSource' else 'channelId')
    channel_id = requested_id or tune['id']
    origin = tune['streams'][0]['metadata']['live']['live']
    delta = run.epoch - datetime.fromisoformat(origin.replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)

    def rebase(value, key=''):
        if isinstance(value, dict):
            return {k: rebase(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [rebase(v, key) for v in value]
        if key == 'validUntil':
            return stamp(now + timedelta(days=1))
        if key == 'nextLiveUpdateStartTimestamp':
            return stamp(now + timedelta(seconds=5))
        if isinstance(value, str) and (key in ('live', 'tuneStart', 'expiration') or 'timestamp' in key.lower()):
            try:
                return stamp(datetime.fromisoformat(value.replace('Z', '+00:00')) + delta)
            except ValueError:
                pass
        return value

    if endpoint == 'liveUpdate':
        elapsed = max(0, run.clock() - run.created)
        updates = responses.get('liveUpdates', [])
        available = [entry for entry in updates if entry['offset_seconds'] <= elapsed]
        selected = available[-1] if available else (updates[0] if updates else None)
        result = selected['response'] if selected else responses.get('liveUpdate')
        if result is None:
            result = copy.deepcopy(tune['streams'][0]['metadata']['live'])
            result.update(id=tune['id'], type=tune['type'])
        result = rebase(result)
        result['id'] = channel_id
        return result
    result = rebase(tune)
    result['id'] = channel_id
    for stream in result['streams']:
        if stream.get('id') == tune['id']:
            stream['id'] = channel_id
        for url in stream['urls']:
            url['url'] = run.current_url(base, 'master.m3u8')
        live = stream['metadata']['live']
        # Force a tune before the cue instead of seeking to the growing stream's edge.
        live['tuneStart'] = stamp(run.epoch)
        live['live'] = stamp(run.epoch + timedelta(seconds=run.initial_duration + max(0, run.clock() - run.created)))
    return result
