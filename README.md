# live-stream-catalog

Builds and maintains a public catalog of live streaming URLs from platforms such as YouTube, Twitch, Kick, and others.

This project is designed to generate a static `channels.json` file that can be publicly accessed via GitHub without requiring paid hosting.

---

## ⚠️ Disclaimer / Legal Notice

This repository **does not host, stream, or distribute any audiovisual content**.

It only:
- collects publicly accessible streaming pages
- resolves their publicly available streaming endpoints (e.g. HLS manifests)
- organizes them into a structured JSON catalog

All content:
- is served directly by the original platforms (YouTube, Twitch, Kick, etc.)
- remains under the responsibility of the respective content owners and platforms

This project:
- does not bypass paywalls, authentication, or DRM
- does not modify or restream any content
- does not guarantee availability, legality, or licensing of any stream

The generated catalog is provided **for informational and convenience purposes only**.

Users are responsible for ensuring compliance with:
- local laws
- platform terms of service
- copyright regulations

---

## How it works

The system operates in two modes:

### `build`
- loads all configured sources
- resolves all channels
- generates a fresh catalog

### `refresh`
- updates only channels with expired or near-expiry URLs
- reduces load and improves availability

---

## Output files

### `channels.json`

Contains resolved streaming URLs and metadata.

### `channels.meta.json`

Contains execution statistics for monitoring.

---

## Configurable channel sources

The catalog ingests channel-oriented providers through generic,
configuration-driven sources. Supported adapters include Stremio addons,
direct JSON channel catalogs, and script-discovered REST catalogs. Public
models and filenames are not coupled to one addon or provider.

Provider URLs are intentionally absent from repository resources. Configure
them at runtime:

```env
LIVE_ADDON_MANIFEST_URL=https://provider.example/manifest.json
LIVE_JSON_CATALOG_URL=https://provider.example/channels
LIVE_JSON_CATALOG_HEADERS={}
LIVE_REST_CATALOG_URL=https://provider.example/
```

Unset variables disable the corresponding adapter. `LIVE_JSON_CATALOG_HEADERS`
accepts a JSON object for runtime request headers and must be supplied through
the deployment secret/configuration mechanism rather than committed.

The source configuration defines the manifest URL, supported
catalogs and types, pagination/extras, exclusion policy, cache TTLs, and stream
variant naming. The initial policy decisions are:

- exclude explicitly adult/+18 channels;
- do not infer adult content from a substring such as `Adult Swim`;
- keep every useful stream variant and append a normalized suffix such as
  `[4K]`, `[FHD]`, `[HD]`, or `[SD]` to the display name;
- keep a stable logical channel identity and a distinct variant identity;
- rebuild discovery every 6 hours and refresh known expiring streams every
  30 minutes;
- retain the last valid source snapshot for up to 24 hours when discovery
  fails;
- assume playback from Brazil; multi-region validation is not required.

`channels.json` preserves transport metadata instead of flattening every
stream into a bare URL. Optional fields include provider/logical/variant IDs,
request headers, secret references, stream protocol, quality/codec metadata,
resolver metadata, DRM metadata, publication capability, and expiration
timestamps. Sensitive header values and short-lived session cookies are not
serialized to the public artifact.

An explicitly missing platform account/channel is retained as a `removed`
tombstone and is no longer resolved. An offline channel, ended live event,
expired manifest, rate limit, timeout, or server failure remains retryable and
is not classified as removed.

Public output names remain generic. Provider provenance may be retained in
internal metadata and logs for diagnostics, but it must not be required in a
public filename or URL.

---

## Development

Create a local virtual environment and install the project in editable mode:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

Run the fast unit test suite:

```bash
.venv/bin/python -m unittest discover -s tests/unit
```

Run all tests. Network integration tests are skipped unless explicitly enabled:

```bash
.venv/bin/python -m unittest discover -s tests
```

Run network integration tests:

```bash
RUN_INTEGRATION_TESTS=1 .venv/bin/python -m unittest discover -s tests/integration
```

Generate a fresh catalog:

```bash
.venv/bin/python -m live_stream_catalog --max-workers 6 build
```

---

## Project structure

```text
live_stream_catalog/
  models/       # domain models
  io/           # persistence
  sources/      # channel sources
  services/     # build / refresh / resolver
  plugins/      # custom Streamlink plugins
