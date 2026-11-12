# Ad failure replay server

Replay the real audio in `fulladbreak.chlz` with controlled interstitial failures. Python 3.10+ and its standard library are sufficient; no packages or upstream media requests are required.

## Start

```sh
cd ~/Desktop/source/failure-server
python3 server.py
```

The server automatically detects your Mac's active LAN address, listens on all IPv4 interfaces, and prints the Charles mapping destinations and phone replay origin. `--public-base-url` remains available as an optional override.

Open <http://127.0.0.1:8765/>. Select a scenario and click **Create new default run**, then start a fresh playback session. The default pod has two ads; choose nine to replay every captured ad. The dashboard shows request observations and the URLs for that run.

The capture is copied into `captures/fulladbreak.chlz`. The first start imports it into `data/`; subsequent starts reuse the imported media. Both directories are ignored by Git, along with recordings, logs, and Python caches. Source code does not contain captured authorization headers or original token-bearing URLs. Generated beacons stay on this server.

## Scenarios

| Scenario | Injected condition | Recovery to look for in the player |
| --- | --- | --- |
| `healthy` | No injected failure | Both ads finish and primary content continues. |
| `asset_list_error` | HTTP 503 for VAST and standard asset-list requests | Skip the unavailable break and continue primary content. |
| `first_prepare_error` | Invalid HLS at the first ad's master and media playlists | Skip the first ad; the remaining healthy ad and content continue. |
| `last_prepare_error` | Invalid HLS at the last selected ad's playlists | Earlier healthy ads finish; content continues past the failed ad. |
| `all_prepare_error` | Invalid HLS for every selected ad | Skip unavailable ads and continue content. |
| `segment_load_exhausted` | A later ad segment waits for the trigger, then every retry and subsequent segment in that ad fails | Recover past the active failed ad and continue playback. This exposes the active-ad recovery gap; the server does not assert that either implementation passed. |

For active-ad failure, start playback and wait until the first ad is **audibly playing**, then click **Trigger segment failure**. Its first three chunks remain healthy; chunk four and later fail persistently. The server only publishes one rendition, so there is no alternate quality to fall back to. The client's retry policy determines when retries are exhausted.

The pre-trigger wait has a 60-second deadline. If you miss it, requests receive a distinct HTTP 408 rather than silently succeeding. Create a new run and restart playback. A client with an especially short load timeout may time out before the manual trigger: watch the dashboard and trigger promptly. Request arrival alone is not proof that playback started.

A fatal renderer failure needs app-side injection. An HTTP replay server cannot faithfully reproduce that error type.

## Use with the SXM app and Charles

Keep **SXM app** mode for comparison of the custom and Media3 implementations. It emits the same internal `sxmp://interstitial/req` asset-list URI and prefetch markers as the Android harness. The app resolves that URI through its existing ads integration; Charles redirects the resulting VAST request here.

Launch normally and configure the three Charles mappings below. The server detects and prints a directly reachable LAN replay origin:

```sh
python3 server.py
```

Your phone and Mac must be able to reach each other on port 8765, and the Android debug build must allow cleartext HTTP. Alternatively, with USB and `adb reverse tcp:8765 tcp:8765`, use `http://127.0.0.1:8765` as the public origin. Direct HTTPS is available using `--tls-cert`, `--tls-key`, and an HTTPS public origin trusted by the phone.

Add these Map Remote entries in Charles (keep the existing SSL Proxying/trusted-certificate setup):

| Source | Destination |
| --- | --- |
| `https://api.edge-gateway.siriusxm.com/playback/play/v1/tuneSource` | `http://127.0.0.1:8765/playback/play/v1/tuneSource` |
| `https://api.edge-gateway.siriusxm.com/playback/play/v1/liveUpdate` | `http://127.0.0.1:8765/playback/play/v1/liveUpdate` |
| `https://siriusxmdigital.deliveryengine.adswizz.com/vast/4.1/request` | `http://127.0.0.1:8765/vast.xml` |

TuneSource and LiveUpdate are POST endpoints. Query strings do not affect routing; allow any query parameters in the Charles mappings. Enable SSL Proxying for both source hosts. `/tuneSource` and `/liveUpdate` aliases also work. Tune any channel to run the replay. TuneSource and LiveUpdate echo the requested channel ID, while the audio and descriptive metadata come from the recording.

TuneSource preserves the captured response schema and replaces **both primary and secondary URLs** with the stable `/current/master.m3u8` URL. It refreshes URL expiry, removes the original encryption-key prefetch ID, rebases episode/cut dates to the replay timeline, and sets `tuneStart` before the cue. The HLS playlist still supplies its own local key URLs. LiveUpdate replays all captured response snapshots at their original offsets relative to TuneSource, with the same rebased timeline and a fresh next-update timestamp; the recorded LiveUpdate response does not itself include stream URLs. TuneSource, LiveUpdate, the mapped AdsWizz endpoint, and every `/current/` playlist, key, media, and beacon request resolve the current default run when the request arrives. Switching the dashboard scenario changes subsequent requests through URLs the app already received. Responses already in flight and audio already buffered are not replaced; restart playback for a clean scenario and timeline.

**AdsWizz is a separate request:** TuneSource redirects streaming URLs but does not change the app's separately configured AdsWizz host. The third mapping above is required to reproduce the local ad-failure scenarios with normal ad settings.

**Alternative to the AdsWizz mapping:** To keep only the two playback API mappings, set these existing Tuner values in `ads_sdk_config` instead:

```json
{
  "isForceTestEnabled": true,
  "forceTestUrl": "http://YOUR_MAC_IP:8765/vast.xml"
}
```

Use the same reachable origin as above. This routes the app's ad SDK to this server while keeping SXM asset resolution in both implementations. Restore these overrides after testing. Use either the AdsWizz mapping or this Tuner override. Without either, ads still come from the real ad service and local ad-failure scenarios will not reproduce.

Create a default run in the dashboard and start playback again for each comparison. Generated playlist, media, key, and beacon URLs all use the public origin, so no stream/asset/key Charles mappings are needed. Confirm requests appear in the dashboard before evaluating recovery.

If direct phone access is unavailable, the previous proxy-only setup remains supported: map `https://failure-server.test/*` to `http://127.0.0.1:8765` with an empty destination path, and run with `--public-base-url https://failure-server.test`. That option adds another routing rule. You can also keep the older original-master and AdsWizz mappings instead of using the playback API endpoints. Path-preserving aliases for captured `mv_long.m3u8`, `*_slide_long.m3u8`, and `/4.1/request` remain available.

Charles supports mapping directories and HTTPS sources to HTTP destinations; its [Map Remote documentation](https://www.charlesproxy.com/documentation/tools/map-remote/) explains the source and destination fields.

For a standalone player using Media3's standard HLS interstitial support, choose **Standard Media3 HLS asset-list JSON** mode and open the run's master URL. That bypasses the SXM ads resolver and exercises `ASSETS` JSON directly. It is a separate integration mode, so use SXM mode when comparing the app implementations. The asset-list format follows Apple's [HLS interstitial guide](https://developer.apple.com/streaming/GettingStartedWithHLSInterstitials.pdf).

For direct device access without Charles, use a reachable origin with `--host 0.0.0.0 --public-base-url http://YOUR_MAC_IP:8765`, or `adb reverse tcp:8765 tcp:8765` with an origin of `http://127.0.0.1:8765`. Android must permit cleartext HTTP for this option. Optional `--tls-cert` and `--tls-key` enable HTTPS directly; also supply an HTTPS `--public-base-url`.

## API and advanced faults

Create a run without changing Charles' default routes:

```sh
curl -s http://127.0.0.1:8765/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"segment_load_exhausted","ad_count":2}'
```

Use `/api/default` instead to make `/master.m3u8`, `/vast.xml`, and `/asset-list.json` use the new run. App-facing URLs use `/current/` and follow whichever run is selected. Explicit `/runs/<id>/...` URLs from `/api/runs` remain available for isolated experiments. Changing the default does not mutate requests already in flight. Up to 100 runs are kept; the oldest run is closed when that limit is reached.

```sh
curl -X POST http://127.0.0.1:8765/api/runs/RUN_ID/trigger
curl -s http://127.0.0.1:8765/api/runs/RUN_ID
curl -X DELETE http://127.0.0.1:8765/api/runs/RUN_ID
```

Configuration fields accepted when creating a run:

| Field | Default | Meaning |
| --- | --- | --- |
| `scenario` | `healthy` | One of the scenarios above. |
| `mode` | `sxm` | `sxm` or `standard`. |
| `ad_count` | `2` | First N captured ads, up to nine. |
| `pre_roll_segments` | `1` | Primary segments before the break. |
| `window_segments` | `0` | Growing EVENT playlist; positive values enable a sliding live window. |
| `fault_ad_index` | `0` | Zero-based ad targeted by segment failure. |
| `fault_segment_index` | `3` | Zero-based first failing segment; must exist and be later than segment zero. |
| `error_status` | `503` | HTTP error for ad resolution and active segment failures. |
| `transport` | `http` | Active segment faults: `http`, immediate `disconnect`, or `timeout` followed by disconnect. |
| `timeout_seconds` | `30` | Delay for the `timeout` transport. |
| `gate_timeout_seconds` | `60` | Maximum wait for a manual active-ad trigger. |
| `failure_count` | `null` | Persistent active segment failures; a positive integer instead fails only N requests in total. |
| `start_armed` | `false` | Begin failing target segments immediately; useful for automation, but does not guarantee an active-ad error. |

Example: target the second ad and disconnect every request from its fourth chunk onward:

```json
{"scenario":"segment_load_exhausted","fault_ad_index":1,"fault_segment_index":3,"transport":"disconnect"}
```

`POST /playback/play/v1/tuneSource` and `POST /playback/play/v1/liveUpdate` serve the captured playback APIs. `GET /api/capture` shows sanitized import details; `GET /api/scenarios` lists scenarios; `GET /health` checks availability. Byte ranges and HEAD are supported. HEAD does not arm, wait for, or consume segment faults.

## What is replayed

The capture contains nine complete ad renditions, fourteen primary-content segments, and one AES-128 key response. Original live playlists reference much more content than the recording actually downloaded. This server rebuilds a growing playlist from the available content, repeats captured segments as necessary, and rebases program dates and the break. Each primary segment retains its original AES IV; discontinuities separate captured timestamp ranges.

Ad AAC is split at ADTS frame boundaries into approximately two-second segments, without re-encoding. Recorded ID3 transport timestamps are removed so segment timestamps can be regenerated by the player. This makes a later-segment failure possible even for the first captured ad, which originally had just one segment. Only the highest fully captured rendition is published.

This is a controlled reproduction using recorded audio, not byte-for-byte playback of the original live manifest. The growing EVENT default makes the break reachable from the start; the optional sliding window allows separate live-window experiments. A fresh run has a fresh timeline and fault counters.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 server.py --import-only
```

The tests exercise real HTTP requests: every scenario, manual gating, persistent and finite failures, run isolation, disconnects/timeouts, byte ranges, playlist growth, VAST rewriting, and Charles import with AES key handling. They verify what the server supplies; they do not certify Android recovery behavior. Recorded ad and encrypted primary audio were additionally decoded with FFmpeg during setup.

Architecture: `capture.py` imports private recordings; `playback.py` rewrites TuneSource and LiveUpdate fixtures; `replay.py` owns each run and generates playlists/fault decisions; `http.py` routes HTTP and the control API; `control.html` is the local dashboard. `server.py` is the entry point.

### Captured metadata and AdsWizz routing

The recording contains 32 LiveUpdate responses spanning roughly four minutes. The importer retains all snapshots, including removals of expired items. Each run selects the latest snapshot whose captured offset has elapsed; after the recording ends it retains the last snapshot. The newest item is identical in every recorded LiveUpdate response, so this particular capture does not provide a sequence of new titles. The server does not invent title changes.

The captured AdsWizz endpoint is `/vast/4.1/request` (including the initial `/vast`). Map that request to `http://127.0.0.1:8765/vast.xml`, allowing any query string. The server accepts `/vast.xml`, `/vast`, `/request`, `/vast/request`, `/4.1/request`, and `/vast/4.1/request`, including trailing slashes and GET or POST requests. The aliases all use the current run and its selected error scenario. Existing imported fixtures are upgraded automatically on startup; no media reimport or deletion is necessary.

For Charles **Map To**, enter Protocol `http`, Host `127.0.0.1`, Port `8765`, and Path `/vast.xml` (with the leading slash). The complete destination must be `http://127.0.0.1:8765/vast.xml`; `http://127.0.0.1:8765vast.xml` is malformed. Allow the incoming query parameters to pass through.
