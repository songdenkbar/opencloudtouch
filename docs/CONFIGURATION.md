# Configuration Reference

OpenCloudTouch is configured via `OCT_`-prefixed environment variables (case-insensitive), read by `AppConfig` in [`apps/backend/src/opencloudtouch/core/config.py`](../apps/backend/src/opencloudtouch/core/config.py). This page documents every variable; see [README.md](../README.md#configuration) for the short version.

## Stereo pairs (SoundTouch 10)

**Experimental backend-only implementation.** Stereo pairs
are separate from multi-room zones. Only two SoundTouch 10 speakers are supported:
LEFT is the master and RIGHT plays the right channel. OCT discovers pairs through
GET /getGroup; no stereo pair database records are created.

To create a pair, both speakers must be present in OCT, available on the LAN,
outside multi-room zones and existing stereo pairs, and have permanent SSH
access enabled. The backend backs up the persistent file at
/mnt/nv/BoseApp-Persistence/1/GroupService.xml, stages LEFT/RIGHT XML,
commits both files, reboots both speakers, and verifies the reported group.
Creating, removing and recovering a pair are long-running jobs (HTTP 202),
polled with GET /api/stereo-pairs/jobs/{job_id}.
The create/remove operation has a six-minute deadline, followed by a separate
four-minute rollback budget if it fails. OCT polls device /info during reboot
and verifies the final pair through /getGroup. An interrupted or timed-out
recovery must be investigated before starting another stereo operation.

On each affected speaker a job may leave:
- GroupService.xml.oct-job — a JSON journal
- GroupService.xml.oct-bak.<job_id> — original content when present
- GroupService.xml.oct-new.<job_id> — staged update

All stereo file changes use OCT's existing SSH write-access guard, which temporarily
remounts read-only filesystems and restores their previous state. The journal
records whether the original backup has been fully prepared. If preparation
was interrupted, recovery cleans incomplete staging without restoring from
a missing backup.

OCT blocks subsequent writes while such files remain. Use
POST /api/stereo-pairs/recover/{device_id} to restore the previous configuration
from journal and backup, followed by reboot. If the journal indicates that a
pair was already verified, recovery preserves that pair and cleans up remaining
artifacts once the related transaction state has been checked. For interrupted
transactions affecting both speakers, OCT coordinates recovery to avoid
restoring only one side. Before running recovery, verify which speaker has the
interrupted operation.

### Manual recovery when an interrupted job cannot be resumed

Prefer `POST /api/stereo-pairs/recover/{device_id}` while both speakers are
reachable. For a shared transaction, OCT checks both journals and coordinates
the decision before deleting either backup. **Do not separately restore or
delete one speaker's files while the other speaker is still in an unresolved
transaction.** A single-speaker recovery is safe only for an incomplete
preparation or a previously verified/restored decision.

If the endpoint cannot complete, stop further stereo operations. Inspect both
speakers over SSH and save the following files to a computer **before any
manual write**, including their hashes:

```sh
# On EACH speaker, via an SSH shell with existing remote_services access:
cd /mnt/nv/BoseApp-Persistence/1
ls -l GroupService.xml GroupService.xml.oct-* 2>/dev/null
cat GroupService.xml.oct-job  # inspect job_id, had_file and state
```

Manual restoration must be based on the matching `job_id` and the journal's
`had_file` value, with both devices' transaction states considered together.
After arranging writable access using OCT's SSH write guard, the equivalent
per-speaker recovery commands are:

```sh
# Use the job_id from the inspected journal (32 lowercase hex characters):
JOB_ID=replace_with_verified_job_id
# Only for an UNVERIFIED transaction, with a matching readable backup:
cp "GroupService.xml.oct-bak.$JOB_ID" GroupService.xml  # had_file=true
# OR, only when the journal explicitly records had_file=false:
rm -f GroupService.xml
```

These are **alternatives**, not commands to run together. Do not use them for
`verified` or `restored` journals, and never remove a backup just because a
write or reboot command returned successfully. Both speakers must be restarted
and their `GET /getGroup` state verified against their pre-job states before
deleting the journal and backup/staged artifacts. If either speaker is
unreachable or the correct backup is missing, retain the artifacts and seek
assistance rather than guessing. The application-managed recovery endpoint
remains the normal and safer method.

**Hardware verification (2026-11-14):** On two SoundTouch 10 speakers running
firmware 27.0.6, create, consistent LEFT/RIGHT group reporting, audible stereo
playback, removal, and restoration to the original absent-file state were
confirmed. A deliberate power interruption of LEFT during `waiting_for_devices`
produced `stereo_timeout` with `rolled_back: false`. After reconnecting,
both device journals were present (LEFT `prepared`, RIGHT `restored`);
coordinated recovery through the API succeeded and a final independent
snapshot confirmed both original files absent, empty `/getGroup` and
`/getZone`, and no remaining transaction artifacts. The existing unrelated
stereo pair remained intact. This does not validate other firmware revisions.

**Remaining hardware checks:** A fresh create/paired/remove/final cycle
*after* recovery is still pending. A backend-restart recovery scenario is
not independently proven unless the development server restart is verified;
the read-only `after-restart` runner alone does not prove a restart.
Multi-room zone creation and automatic grouping are not prevented for
stereo-pair members; do not run those actions during stereo jobs.
Devices may appear offline while rebooting. Mock tests do not replace
real channel-separation tests.

For an optional **manual rollback test**, the script
`apps/backend/scripts/test_stereo_hardware_rollback.py` accepts explicit
`--left-id`, `--left-ip`, `--right-id`, `--right-ip`,
`--left-original` and `--right-original` arguments. Each original must be
either the literal `absent` or an existing local byte-for-byte file backup.
The script has no built-in device addresses or backup checksums. By default it
performs a **read-only preflight**. Only an additional explicit `--execute`
enables the temporary on-device write and injected rollback failure.
Run it only on two deliberately unpaired speakers with empty /getGroup and
/getZone responses, verified SSH access and a reliable restoration plan.
Never repeat the write test after any failure.

For physical outage, OCT-restart recovery, and post-recovery
regression procedures, follow
[`STEREO_HARDWARE_TEST_PLAN.md`](../apps/backend/scripts/STEREO_HARDWARE_TEST_PLAN.md)
using the read-only-by-default `test_stereo_hardware_scenarios.py` runner.
It requires explicit `--execute` for any API write/reboot operation; power
disconnection and OCT restart remain manual.

## Server

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_HOST` | `0.0.0.0` | API bind address |
| `OCT_PORT` | `7777` | API port |
| `OCT_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` \| `CRITICAL` |
| `OCT_LOG_FORMAT` | `text` | `text` or `json` |
| `OCT_LOG_FILE` | unset | Optional log file path |
| `OCT_PERF_LOG` | unset | Optional file path; every device-operation timing (SSH connect, command, backup step) is appended as one JSON line, in addition to the `PERF` lines in the backend log |
| `OCT_LOG_DIR` | unset | Directory for persistent clustered log files (e.g. `/logs`); if set, each log cluster writes a `RotatingFileHandler` here |

## CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_CORS_ORIGINS` | localhost dev ports + `OCT_PORT` | JSON array of allowed origins. Use `["*"]` for development only. The configured `OCT_PORT` is automatically appended if not already present. |

## Mock Mode & Database

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_MOCK_MODE` | `false` | Enable mock mode (no real devices required — useful for UI development) |
| `OCT_DB_PATH` | auto | SQLite database path. Empty = auto-configured: `:memory:` when `CI=true`, `data-local/oct-test.db` in mock mode, otherwise `/data/oct.db` |

## Discovery & Device Polling

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_DISCOVERY_ENABLED` | `true` | Enable SSDP/UPnP discovery |
| `OCT_DISCOVERY_TIMEOUT` | `3` | Discovery timeout (seconds) |
| `OCT_MANUAL_DEVICE_IPS` | `""` | Comma-separated fallback IPs, used when SSDP discovery doesn't find a device (e.g. different subnet, discovery disabled) |
| `OCT_DEVICE_POLLING_ENABLED` | `true` | Enable the periodic background alive-polling task (ping every 5 min, SSH BMX verify every 30 min, zone sync every 15 min). Disable if your devices are mostly offline and the polling noise/load is unwanted. |

## Device Ports & Station Descriptor

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_DEVICE_HTTP_PORT` | `8090` | SoundTouch device HTTP API port |
| `OCT_DEVICE_WS_PORT` | `8080` | SoundTouch device WebSocket port |
| `OCT_STATE_CACHE_MAX_AGE` | `10.0` | Max age (seconds) for WebSocket-fed state cache before falling back to an HTTP poll |
| `OCT_STATION_DESCRIPTOR_BASE_URL` | `http://localhost:7777` | Base URL OCT advertises to devices for preset programming. If left as `localhost`, it's automatically replaced with `content.api.bose.io:<port>` (devices resolve this via `/etc/hosts` redirect) since devices can't reach the server's own `localhost`. |

## Bug Reports & Production Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `OCT_GITHUB_TOKEN` | `""` | GitHub token used to create bug-report issues from the UI (optional) |
| `OCT_GITHUB_REPO` | `""` | `owner/repo` target for bug reports (optional) |
| `OCT_ALLOW_DANGEROUS_OPERATIONS` | `false` | Allow destructive endpoints such as `DELETE /api/devices` — testing only, do not enable in production |

## Docker Build Arguments

These are **build-time** (`docker build --build-arg ...`), not runtime environment variables — set once when the image is built, baked into the image afterward.

| Build Arg | Description |
|-----------|-------------|
| `OCT_BUILD_SIGNATURE` | HMAC-SHA256 signature of the version, produced only by CI. Marks a build as "official" (`is_official_build()`, exposed as `build: "official"` on `/health`). Cannot be produced locally — self-built images are always `"community"`. |
| `OCT_VERSION` | Optional override for the reported version string. Not required for a correct version — the real `pyproject.toml` version is already baked into every image via package metadata, official or self-built. Only useful for forks that want to stamp a custom version string. Example: `docker build --build-arg OCT_VERSION=my-fork-2.0.0 ...` |

See [`deployment/README.md`](../deployment/README.md#build-arguments) for build examples.

## Also See

- [Troubleshooting](TROUBLESHOOTING.md)
- [`.env` file structure](../ENV-FILES.md) — which `.env` file to use for local dev vs. deployment
