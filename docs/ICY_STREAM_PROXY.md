# ICY Stream Proxy

OpenCloudTouch can optionally proxy custom internet-radio streams so ICY metadata is read from the same connection that carries the audio.

The proxy can extract metadata without opening its own separate metadata-only connection and allows Bose SoundTouch devices to receive track information through the existing BMX now-playing endpoints. Existing OCT polling remains a separate mechanism and is unchanged by this feature.

## Enable the proxy

Set `OCT_STREAM_PROXY_URL` to an address that the SoundTouch devices can reach:

```bash
OCT_STREAM_PROXY_URL=http://<oct-host>:7789/stream
```

The setting is opt-in. If it is unset, stream playback behaves exactly as before and the proxy server is not started.

The host in this URL must be reachable from the SoundTouch devices. Do not use `localhost` unless the device itself resolves that name to the OCT host.

## How it works

```text
SoundTouch device
      |
      | GET /stream?url=<radio-stream>
      v
OpenCloudTouch ICY proxy
      |
      | Icy-MetaData: 1
      v
Radio stream server
```

The proxy:

1. opens the upstream HTTP/HTTPS stream with `Icy-MetaData: 1`;
2. reads the `icy-metaint` interval when the station provides it;
3. removes interleaved ICY metadata blocks from the audio stream;
4. forwards only the audio bytes to the SoundTouch device;
5. stores the latest metadata for that device connection;
6. exposes that metadata through the existing BMX now-playing/reporting responses.

The proxy deliberately uses a simple non-chunked HTTP response because SoundTouch devices are more compatible with that form of streaming.

If the upstream server does not provide `icy-metaint`, the proxy transparently passes the stream through unchanged.

## Metadata handling

Metadata is associated with the SoundTouch client IP and the active proxy session.

Session tracking prevents an older stream connection from overwriting or deleting metadata belonging to a newer connection from the same device, for example during a station change.

Known title formats are parsed by the existing ICY metadata parser. If a title cannot be split into artist and track, the raw title is retained and can still be shown by the BMX now-playing response.

## Network and security considerations

The proxy is intended for a trusted local network.

- Only `http://` and `https://` upstream URLs are accepted.
- The proxy listens on all interfaces so SoundTouch devices on the LAN can reach it.
- Do **not** expose the proxy port to the Internet.
- The proxy accepts a target stream URL supplied in the request. On an untrusted network this could be abused to make OCT connect to other HTTP services reachable from the host.
- OpenCloudTouch itself does not add authentication to this local-device flow.

The default proxy port is `7789`. If a different port is included in `OCT_STREAM_PROXY_URL`, the embedded proxy server uses that port.

## Diagnostics

A development-only diagnostic endpoint is available at:

```text
GET /debug/icy-proxy
```

It returns the current in-memory metadata snapshot keyed by device IP. The endpoint is intentionally excluded from the generated OpenAPI schema and is meant for local troubleshooting, not as a public application API.

Example response:

```json
{
  "192.0.2.10": {
    "station_name": "Example Radio",
    "artist": "Aster Vale",
    "track": "Hold the Light",
    "raw_title": "Aster Vale - Hold the Light"
  }
}
```

When playback stops and the active proxy session closes, its cached metadata is removed.

## Relationship to ICY polling

The stream proxy and the existing ICY probing/polling code are separate mechanisms.

This feature does not remove or alter the existing polling architecture. Any change to polling frequency or suppression of redundant probes should be reviewed independently so the proxy can be introduced without changing fallback behavior for non-proxied streams.

## Troubleshooting

If the SoundTouch device cannot play a proxied stream:

- verify that `OCT_STREAM_PROXY_URL` contains the OCT host address reachable from the device;
- verify that the proxy port is reachable on the LAN;
- check the OCT log for `[ICY PROXY]` messages;
- call `/debug/icy-proxy` while a proxied station is playing;
- test the original stream URL directly to distinguish upstream-station problems from proxy problems.

If metadata remains empty while audio plays, the upstream station may not provide ICY metadata. In that case passthrough playback is expected.
