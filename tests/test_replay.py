import base64
import concurrent.futures
import copy
import http.client
import json
import io
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from failure_server.capture import import_capture, split_adts
from failure_server.http import ReplayServer, detect_lan_address, main
from failure_server.playback import save_playback_fixture
from failure_server.capture import Transaction
from failure_server.replay import Config, Run


def frame():
    length = 11
    return bytes([0xff, 0xf1, 0x50, (length >> 11) & 3,
                  (length >> 3) & 255, ((length & 7) << 5) | 0x1f, 0xfc]) + b'abcd'


def fixture(root):
    (root / 'media').mkdir()
    (root / 'keys').mkdir()
    (root / 'keys/0.bin').write_bytes(b'0123456789abcdef')
    (root / 'media/content.aac').write_bytes(b'captured-primary-content')
    ads = []
    for i in range(2):
        segments = []
        for j in range(6):
            name = f'media/ad-{i}-{j}.aac'
            (root / name).write_bytes(frame() * 4)
            segments.append({'file': name, 'duration': 2.0})
        ads.append({'id': f'ad-{i+1}', 'duration': 12.0, 'segments': segments})
    return {'format': 1, 'capture_name': 'fixture.chlz', 'capture_sha256': 'fixture', 'transactions': 12,
            'ads': ads, 'content': [{'file': 'media/content.aac', 'duration': 10.0, 'key': '0', 'iv': '0x'+'01'*16}],
            'keys': ['keys/0.bin'], 'warnings': []}


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = fixture(self.root)
        self.server = ReplayServer(('127.0.0.1', 0), self.catalog, self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        if body is not None:
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def new_run(self, **config):
        status, _, body = self.request('/api/runs', 'POST', config)
        self.assertEqual(status, 201, body)
        return json.loads(body)

    def playback_fixture(self):
        live = {'live': '2020-01-01T00:00:00Z', 'tuneStart': None,
                'nextLiveUpdateStartTimestamp': '2020-01-01T00:00:05Z',
                'items': [{'id': 'cut', 'timestamp': '2020-01-01T00:00:10Z', 'duration': 12000}],
                'episodes': [{'startTimestamp': '2019-12-31T23:00:00Z', 'duration': 3600000}]}
        tune = {'id': 'channel', 'type': 'channel-linear', 'streams': [
            {'id': 'stream', 'urls': [
                {'name': name, 'url': 'https://original.invalid/SECRET', 'isPrimary': name == 'primary',
                 'validUntil': '2020-01-02T00:00:00Z', 'encryptionKeyId': 'old-key'}
                for name in ('primary', 'secondary')], 'metadata': {'live': live}}]}
        update = {**copy.deepcopy(live), 'id': 'channel', 'type': 'channel-linear'}
        save_playback_fixture([
            Transaction(1, 'https://api.invalid/playback/play/v1/tuneSource', json.dumps(tune).encode(), 'application/json'),
            Transaction(2, 'https://api.invalid/playback/play/v1/liveUpdate', json.dumps(update).encode(), 'application/json')
        ], self.root)
        self.assertNotIn('SECRET', (self.root / 'playback.json').read_text())

    def test_tune_source_routes_all_streams_to_selected_run_and_refreshes_metadata(self):
        from datetime import datetime, timezone
        self.playback_fixture()
        run = self.new_run(scenario='first_prepare_error')
        self.server.default_id = run['id']
        status, _, body = self.request('/playback/play/v1/tuneSource?ignored=true', 'POST', {'id': 'channel'})
        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertEqual(response['id'], 'channel')
        for stream in response['streams']:
            for url in stream['urls']:
                self.assertEqual(url['url'], self.origin + '/current/master.m3u8')
                self.assertIsNone(url['encryptionKeyId'])
                self.assertGreater(datetime.fromisoformat(url['validUntil'].replace('Z', '+00:00')), datetime.now(timezone.utc))
                self.assertEqual(self.request(urlsplit(url['url']).path)[0], 200)
            live = stream['metadata']['live']
            start = datetime.fromisoformat(live['tuneStart'].replace('Z', '+00:00'))
            cut = datetime.fromisoformat(live['items'][0]['timestamp'].replace('Z', '+00:00'))
            self.assertEqual((cut - start).total_seconds(), 10)
            self.assertEqual(live['items'][0]['duration'], 12000)
        self.assertEqual(self.request(f"/runs/{run['id']}/ads/0/master.m3u8")[2], b'injected invalid HLS playlist\n')

    def test_live_update_preserves_schema_and_uses_same_timeline_without_consuming_runs(self):
        self.playback_fixture()
        status, _, tune_body = self.request('/tuneSource', 'POST', {'id': 'channel'})
        self.assertEqual(status, 200)
        tune = json.loads(tune_body)
        for path in ('/playback/play/v1/liveUpdate', '/liveUpdate'):
            status, _, body = self.request(path, 'POST', {'channelId': 'channel', 'startTimestamp': '2026-01-01T00:00:00Z'})
            self.assertEqual(status, 200)
            live = json.loads(body)
            self.assertNotIn('streams', live)
            self.assertEqual(live['items'], tune['streams'][0]['metadata']['live']['items'])
            self.assertEqual(live['episodes'], tune['streams'][0]['metadata']['live']['episodes'])
            self.assertNotIn('2020-', live['nextLiveUpdateStartTimestamp'])
        self.assertEqual(len(self.server.runs), 1)
        self.assertEqual(self.server.default_run().counts['liveUpdate'], 2)

    def test_playback_requests_accept_any_channel_and_preserve_request_identity(self):
        self.assertEqual(self.request('/tuneSource', 'POST', {})[0], 400)
        self.playback_fixture()
        fixture = json.loads((self.root / 'playback.json').read_text())
        fixture['tuneSource']['streams'][0]['id'] = 'channel'
        (self.root / 'playback.json').write_text(json.dumps(fixture))
        for channel in ('other', 'another-channel', 'channel'):
            with self.subTest(channel=channel):
                status, _, body = self.request('/tuneSource', 'POST', {'id': channel})
                self.assertEqual(status, 200)
                tune = json.loads(body)
                self.assertEqual(tune['id'], channel)
                self.assertEqual(tune['streams'][0]['id'], channel)
                self.assertEqual(self.request(urlsplit(tune['streams'][0]['urls'][0]['url']).path)[0], 200)
                status, _, body = self.request('/liveUpdate', 'POST', {'channelId': channel})
                self.assertEqual(status, 200)
                live = json.loads(body)
                self.assertEqual(live['id'], channel)
                self.assertEqual(live['items'], tune['streams'][0]['metadata']['live']['items'])
        self.assertEqual(json.loads((self.root / 'playback.json').read_text())['tuneSource']['id'], 'channel')

    def test_playback_urls_use_configured_phone_reachable_origin(self):
        self.playback_fixture()
        self.server.public_base = 'https://replay.example'
        status, _, body = self.request('/tuneSource', 'POST', {})
        self.assertEqual(status, 200)
        url = json.loads(body)['streams'][0]['urls'][0]['url']
        self.assertEqual(url, 'https://replay.example/current/master.m3u8')

    def test_cached_app_urls_follow_current_run_after_scenario_switch(self):
        from datetime import timedelta
        self.playback_fixture()
        first = json.loads(self.request('/api/default', 'POST', {'mode': 'standard'})[2])
        tune = json.loads(self.request('/tuneSource', 'POST', {'id': 'any-channel'})[2])
        master_path = urlsplit(tune['streams'][0]['urls'][0]['url']).path
        self.assertEqual(master_path, '/current/master.m3u8')
        master = self.request(master_path)[2].decode()
        playlist_path = urlsplit(next(line for line in master.splitlines() if line.startswith('http'))).path
        playlist = self.request(playlist_path)[2].decode()
        self.assertIn('/current/asset-list.json', playlist)
        self.assertNotIn('/runs/', playlist)
        assets = json.loads(self.request('/current/asset-list.json')[2])['ASSETS']
        ad_path = urlsplit(assets[0]['URI']).path
        self.assertEqual(self.request(ad_path)[0], 200)
        ad_master = self.request(ad_path)[2].decode()
        self.assertTrue(ad_master.startswith('#EXTM3U'))
        ad_media_path = urlsplit(next(line for line in ad_master.splitlines() if line.startswith('http'))).path
        ad_media = self.request(ad_media_path)[2].decode()
        segment_paths = [urlsplit(line).path for line in ad_media.splitlines() if line.startswith('http')]
        self.assertTrue(all(path.startswith('/current/') for path in segment_paths))
        vast = self.request('/vast.xml')[2]
        self.assertIn(b'/current/ads/0/master.m3u8', vast)
        self.assertNotIn(b'/runs/', vast)
        second = json.loads(self.request('/api/default', 'POST', {'scenario': 'first_prepare_error', 'mode': 'standard'})[2])
        current = self.server.runs[second['id']]
        current.epoch += timedelta(seconds=10)
        # URLs obtained before switching resolve the new scenario without another tune.
        self.assertEqual(self.request(ad_path)[2], b'injected invalid HLS playlist\n')
        self.assertIn('/current/live.m3u8', self.request(master_path)[2].decode())
        update = json.loads(self.request('/liveUpdate', 'POST', {'channelId': 'any-channel'})[2])
        fresh_tune = json.loads(self.request('/tuneSource', 'POST', {'id': 'any-channel'})[2])
        self.assertEqual(update['items'], fresh_tune['streams'][0]['metadata']['live']['items'])
        self.assertNotEqual(update['items'], tune['streams'][0]['metadata']['live']['items'])
        self.assertEqual(current.counts['liveUpdate'], 1)
        self.assertNotIn('liveUpdate', self.server.runs[first['id']].counts)
        # Explicit run URLs remain available for isolated API experiments.
        self.assertTrue(self.request(f"/runs/{first['id']}/ads/0/master.m3u8")[2].startswith(b'#EXTM3U'))
        self.request('/api/default', 'POST', {'scenario': 'segment_load_exhausted', 'start_armed': True})
        self.assertEqual(self.request(segment_paths[0])[0], 200)
        self.assertEqual(self.request(segment_paths[3])[0], 503)
        self.assertEqual(self.request(segment_paths[3])[0], 503)

    def test_live_updates_replay_every_captured_snapshot_by_elapsed_time(self):
        self.playback_fixture()
        original = json.loads((self.root / 'playback.json').read_text())
        tune = original['tuneSource']
        first = original['liveUpdate']
        second = copy.deepcopy(first)
        second['items'].append({'id': 'later-cut', 'timestamp': '2020-01-01T00:00:20Z', 'duration': 10000})
        third = copy.deepcopy(second)
        third['items'] = third['items'][1:]
        save_playback_fixture([
            Transaction(1, 'https://api.invalid/tuneSource', json.dumps(tune).encode(), 'application/json', '2020-01-01T00:00:00Z'),
            Transaction(2, 'https://api.invalid/liveUpdate', json.dumps(first).encode(), 'application/json', '2020-01-01T00:00:00Z'),
            Transaction(3, 'https://api.invalid/liveUpdate', json.dumps(second).encode(), 'application/json', '2020-01-01T00:00:10Z'),
            Transaction(4, 'https://api.invalid/liveUpdate', json.dumps(third).encode(), 'application/json', '2020-01-01T00:00:20Z')
        ], self.root)
        fixture = json.loads((self.root / 'playback.json').read_text())
        self.assertEqual([entry['offset_seconds'] for entry in fixture['liveUpdates']], [0, 10, 20])
        run = self.server.default_run()
        elapsed = [0]
        run.clock = lambda: elapsed[0]
        run.created = 0
        for when, ids in [(0, ['cut']), (9, ['cut']), (10, ['cut', 'later-cut']), (20, ['later-cut']), (100, ['later-cut'])]:
            elapsed[0] = when
            status, _, body = self.request('/liveUpdate', 'POST', {'channelId': 'any-channel'})
            self.assertEqual(status, 200)
            response = json.loads(body)
            self.assertEqual(response['id'], 'any-channel')
            self.assertEqual([item['id'] for item in response['items']], ids)

    def test_vast_routes_accept_original_and_mapped_paths_and_follow_current_scenario(self):
        paths = ('/vast.xml', '/vast', '/request', '/vast/request', '/4.1/request', '/vast/4.1/request', '/vast/4.1/request/')
        for method in ('GET', 'POST'):
            for path in paths:
                with self.subTest(method=method, path=path):
                    status, _, body = self.request(path + '?adEvents=ignored', method=method)
                    self.assertEqual(status, 200)
                    self.assertEqual(ET.fromstring(body).tag, 'VAST')
        self.request('/api/default', 'POST', {'scenario': 'asset_list_error'})
        for path in paths:
            self.assertEqual(self.request(path)[0], 503)

    def test_startup_defaults_detect_origin_print_mappings_and_use_bound_port(self):
        (self.root / 'catalog.json').write_text(json.dumps(self.catalog))
        output = io.StringIO()
        with patch('sys.argv', ['server.py', '--data', str(self.root), '--capture', str(self.root / 'absent.chlz'), '--port', '0']), \
                patch('failure_server.http.detect_lan_address', return_value='192.168.1.22'), \
                patch.object(ReplayServer, 'serve_forever', side_effect=KeyboardInterrupt), redirect_stdout(output):
            main()
        banner = output.getvalue()
        self.assertIn('Phone replay origin: http://192.168.1.22:', banner)
        self.assertNotIn('192.168.1.22:0', banner)
        self.assertIn('https://api.edge-gateway.siriusxm.com/playback/play/v1/tuneSource -> http://127.0.0.1:', banner)
        self.assertIn('isForceTestEnabled=true', banner)


    def test_healthy_graph_has_only_local_resources_and_valid_keys(self):
        run = self.new_run(mode='standard')
        master = self.request(urlsplit(run['urls']['master']).path)[2].decode()
        live = next(line for line in master.splitlines() if line.startswith('http'))
        playlist = self.request(urlsplit(live).path)[2].decode()
        self.assertIn('X-ASSET-LIST="'+run['urls']['asset_list']+'"', playlist)
        self.assertIn('X-RESUME-OFFSET=24.000000000', playlist)
        self.assertIn('#EXT-X-START:TIME-OFFSET=0', playlist)
        self.assertNotIn('siriusxm.com', playlist)
        self.assertEqual(self.request(f"/runs/{run['id']}/keys/0.bin")[2], b'0123456789abcdef')
        assets = json.loads(self.request(urlsplit(run['urls']['asset_list']).path)[2])['ASSETS']
        self.assertEqual(len(assets), 2)
        for asset in assets:
            status, _, ad_master = self.request(urlsplit(asset['URI']).path)
            self.assertEqual(status, 200)
            media_url = next(line for line in ad_master.decode().splitlines() if line.startswith('http'))
            ad_playlist = self.request(urlsplit(media_url).path)[2].decode()
            self.assertIn('#EXT-X-ENDLIST', ad_playlist)
            for line in ad_playlist.splitlines():
                if line.startswith('http'):
                    self.assertTrue(line.startswith(self.origin))
                    self.assertEqual(self.request(urlsplit(line).path)[0], 200)

    def test_sxm_mode_and_vast_preserve_media_contract_without_external_trackers(self):
        run = self.new_run(mode='sxm')
        playlist = self.request(f"/runs/{run['id']}/live.m3u8")[2].decode()
        self.assertIn('sxmp://interstitial/req?', playlist)
        xml = ET.fromstring(self.request(urlsplit(run['urls']['vast']).path)[2])
        self.assertEqual(len(xml.findall('Ad')), 2)
        for element in xml.findall('.//MediaFile') + xml.findall('.//Tracking') + xml.findall('.//Impression'):
            self.assertTrue(element.text.startswith(self.origin))

    def test_request_failure_affects_both_ad_resolution_endpoints_only(self):
        run = self.new_run(scenario='asset_list_error')
        for endpoint in ('vast.xml', 'asset-list.json'):
            self.assertEqual(self.request(f"/runs/{run['id']}/{endpoint}")[0], 503)
        self.assertEqual(self.request(f"/runs/{run['id']}/content/0.aac")[0], 200)

    def test_prepare_scenarios_target_first_last_or_all_ads(self):
        for scenario, broken in [('first_prepare_error', [0]), ('last_prepare_error', [1]), ('all_prepare_error', [0, 1])]:
            run = self.new_run(scenario=scenario)
            for index in (0, 1):
                for playlist in ('master', 'media'):
                    body = self.request(f"/runs/{run['id']}/ads/{index}/{playlist}.m3u8")[2]
                    self.assertEqual(body.startswith(b'#EXTM3U'), index not in broken)
            self.assertEqual(self.request(f"/runs/{run['id']}/content/0.aac")[0], 200)

    def test_active_fault_waits_for_trigger_then_fails_every_retry(self):
        run = self.new_run(scenario='segment_load_exhausted')
        path = f"/runs/{run['id']}/ads/0/segments/3.aac"
        self.assertEqual(self.request(f"/runs/{run['id']}/ads/0/segments/0.aac")[0], 200)
        # HEAD does not release, consume, or block on the active fault.
        self.assertEqual(self.request(path, 'HEAD')[0], 200)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(self.request, path)
            deadline = time.monotonic()+3
            while True:
                status = json.loads(self.request(f"/api/runs/{run['id']}")[2])
                if status['waiting_segments']:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertFalse(pending.done())
            self.assertEqual(self.request(f"/api/runs/{run['id']}/trigger", 'POST', {})[0], 200)
            self.assertEqual(pending.result()[0], 503)
        self.assertEqual(self.request(path, headers={'Range': 'bytes=2-'})[0], 503)
        self.assertEqual(self.request(f"/runs/{run['id']}/ads/0/segments/4.aac")[0], 503)
        self.assertEqual(self.request(f"/runs/{run['id']}/ads/1/segments/3.aac")[0], 200)
        self.assertEqual(self.request(f"/runs/{run['id']}/content/2.aac")[0], 200)

    def test_runs_are_isolated_and_finite_fault_can_recover(self):
        faulty = self.new_run(scenario='segment_load_exhausted', start_armed=True, failure_count=1)
        healthy = self.new_run()
        suffix = 'ads/0/segments/3.aac'
        self.assertEqual(self.request(f"/runs/{faulty['id']}/{suffix}")[0], 503)
        self.assertEqual(self.request(f"/runs/{healthy['id']}/{suffix}")[0], 200)
        self.assertEqual(self.request(f"/runs/{faulty['id']}/{suffix}")[0], 200)

    def test_disconnect_and_timeout_close_connection(self):
        for transport in ('disconnect', 'timeout'):
            run = self.new_run(scenario='segment_load_exhausted', start_armed=True,
                               transport=transport, timeout_seconds=.01)
            with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError)):
                self.request(f"/runs/{run['id']}/ads/0/segments/3.aac")

    def test_binary_range_requests_and_unknown_resources(self):
        run = self.new_run()
        path = f"/runs/{run['id']}/ads/0/segments/0.aac"
        complete = self.request(path)[2]
        status, headers, body = self.request(path, headers={'Range': 'bytes=2-7'})
        self.assertEqual((status, body), (206, complete[2:8]))
        self.assertEqual(headers['Content-Range'], f'bytes 2-7/{len(complete)}')
        self.assertEqual(self.request(path, headers={'Range': 'bytes=-4'})[2], complete[-4:])
        self.assertEqual(self.request(path, headers={'Range': 'bytes=999999-'})[0], 416)
        self.assertEqual(self.request(f"/runs/{run['id']}/keys/999.bin")[0], 404)
        self.assertEqual(self.request('/../../etc/passwd')[0], 404)

    def test_invalid_config_does_not_create_run(self):
        for body in [{'scenario': 'fatal_renderer'}, {'ad_count': 99}, {'gate_timeout_seconds': -1},
                     {'ad_count': True}, {'unknown': 1}, {'fault_segment_index': 999},
                     {'transport': 'upstream'}, {'timeout_seconds': 'slow'}]:
            status, _, _ = self.request('/api/runs', 'POST', body)
            self.assertEqual(status, 400, body)
        self.assertEqual(len(self.server.runs), 0)

    def test_reset_default_creates_fresh_state_without_mutating_old_run(self):
        first = json.loads(self.request('/api/default', 'POST', {'scenario':'first_prepare_error'})[2])
        second = json.loads(self.request('/api/default', 'POST', {'scenario':'healthy'})[2])
        self.assertNotEqual(first['id'], second['id'])
        self.assertIn(b'/current/live.m3u8', self.request('/master.m3u8')[2])
        self.assertEqual(first['urls']['master'], second['urls']['master'])
        self.assertFalse(self.request(f"/runs/{first['id']}/ads/0/master.m3u8")[2].startswith(b'#EXTM3U'))
        self.assertEqual(self.request(f"/api/runs/{first['id']}", 'DELETE')[0], 204)
        self.assertEqual(self.request(f"/api/runs/{first['id']}")[0], 404)

    def test_event_playlist_advances_and_sliding_discontinuity_sequence_is_stable(self):
        now = [100.0]
        run = Run(self.catalog, Config(), self.root, clock=lambda:now[0])
        before = run.playlist(self.origin).body
        now[0] += 35
        after = run.playlist(self.origin).body
        self.assertGreater(after.count(b'#EXTINF:'), before.count(b'#EXTINF:'))
        run.config = Config(window_segments=3)
        sliding = run.playlist(self.origin).body.decode()
        self.assertNotIn('PLAYLIST-TYPE:EVENT', sliding)
        seq = next(l.split(':')[1] for l in sliding.splitlines() if l.startswith('#EXT-X-MEDIA-SEQUENCE:'))
        self.assertIn('#EXT-X-DISCONTINUITY-SEQUENCE:'+seq, sliding)


class AddressDetectionTests(unittest.TestCase):
    def test_mac_uses_default_route_interface_instead_of_assuming_wifi(self):
        from types import SimpleNamespace
        with patch('failure_server.http.sys.platform', 'darwin'), \
                patch('failure_server.http.subprocess.run', side_effect=[
                    SimpleNamespace(stdout='interface: en7\n'), SimpleNamespace(stdout='192.168.2.4\n')]) as command:
            self.assertEqual(detect_lan_address(), '192.168.2.4')
        self.assertEqual(command.call_args.args[0], ['/usr/sbin/ipconfig', 'getifaddr', 'en7'])

    def test_detection_fails_explicitly_when_no_route_is_available(self):
        with patch('failure_server.http.sys.platform', 'linux'), \
                patch('failure_server.http.socket.socket', side_effect=OSError('No route')):
            with self.assertRaisesRegex(ValueError, 'public-base-url'):
                detect_lan_address()



class CaptureTests(unittest.TestCase):
    def test_adts_split_preserves_frames_and_removes_recorded_id3_timestamp(self):
        data = b'ID3\x04\x00\x00\x00\x00\x00\x00' + frame()*500
        chunks = split_adts(data)
        self.assertEqual(b''.join(b for b,d in chunks), frame()*500)
        self.assertAlmostEqual(sum(d for b,d in chunks), 500*1024/44100)
        self.assertGreaterEqual(len(chunks), 5)
        with self.assertRaises(ValueError):
            split_adts(b'not AAC')

    def test_charles_import_resolves_relative_vast_media_and_preserves_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'capture.chlz'
            with zipfile.ZipFile(source,'w') as z:
                index = 0
                def add(host,path,body,mime='application/octet-stream'):
                    nonlocal index
                    name = f'{index}-res.dat'
                    z.writestr(name,body)
                    z.writestr(f'{index}-meta.json',json.dumps({'scheme':'https','host':host,'path':path,'method':'GET',
                        'request':{'header':{'Authorization':'secret'}},
                        'response':{'status':200,'bodyFile':name,'mimeType':mime}}))
                    index += 1
                vast = '<VAST xmlns="urn:test">'+''.join(f'<Ad sequence="{i+1}"><InLine><Creatives><Creative><Linear><MediaFiles><MediaFile type="audio/hls_44">/ad{i}/mv.m3u8</MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad>' for i in range(2))+'</VAST>'
                add('ads.example','/request',vast.encode(),'application/xml')
                for i in range(2):
                    add('media.example',f'/ad{i}/mv.m3u8',b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=256000\n256k/od.m3u8\n')
                    add('media.example',f'/ad{i}/256k/od.m3u8',b'#EXTM3U\n#EXTINF:11.61,\n0.aac\n#EXT-X-ENDLIST\n')
                    add('media.example',f'/ad{i}/256k/0.aac',frame()*500)
                primary = '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:12\n#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example/key"\n#EXT-X-DATERANGE:ID="cue"\n#EXTINF:10,\n0.aac\n'
                add('content.example','/secret/live.m3u8',primary.encode())
                add('content.example','/secret/0.aac',b'encrypted content')
                add('keys.example','/key',json.dumps({'key':base64.b64encode(b'0123456789abcdef').decode()}).encode())
            catalog = import_capture(source,root/'data')
            self.assertEqual(len(catalog['ads']),2)
            self.assertEqual(catalog['content'][0]['iv'],'0x'+f'{12:032x}')
            self.assertEqual((root/'data/keys/0.bin').read_bytes(),b'0123456789abcdef')
            self.assertNotIn('secret',json.dumps(catalog))
            self.assertNotIn('Authorization',json.dumps(catalog))
            self.assertEqual(import_capture(source,root/'data'),catalog)
            with source.open('ab') as stream:
                stream.write(b'changed')
            with self.assertRaises(ValueError):
                import_capture(source,root/'data')


if __name__ == '__main__':
    unittest.main()
