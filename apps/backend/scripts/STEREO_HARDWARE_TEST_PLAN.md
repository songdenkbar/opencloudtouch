# Stereo #549 – hardware acceptance runbook

Manual, reproducible acceptance checks for the experimental SoundTouch 10
backend. No special test script is required: this guide uses the public OCT
REST API, the speaker HTTP API and read-only SSH inspection. Destructive
requests and power interruptions must be initiated deliberately.

## Recorded hardware results (2026-11-14)

Two SoundTouch 10 speakers (firmware 27.0.6) passed create, channel-separated
playback, remove, and restoration of their originally absent
`GroupService.xml` files. During a deliberate LEFT power interruption in
`waiting_for_devices`, OCT reported `stereo_timeout` and
`rolled_back: false`; the journals remained available for recovery.
After reconnecting LEFT, pair-coordinated API recovery succeeded.
A subsequent independent baseline → create → paired → remove → final test
passed. Both speaker files were absent again, `/getGroup` and `/getZone`
were empty, no `oct-*` artifacts remained and a separate, existing stereo
pair was unaffected.

**Still unverified:** recovery after an independently confirmed OCT process
restart during an unfinished transaction, and behavior on other firmware
revisions. Do not describe those scenarios as passed.

## Safety and setup

1. Use only two dedicated SoundTouch 10 test speakers with permanent SSH
   access. Confirm the API below targets the intended **development** server
   and database, not a production instance. Disable competing pairing or
   automatic grouping actions.
2. Both speakers must be absent from multi-room zones and stereo pairs before
   creation. Record firmware, IDs, IP addresses and their original `/getGroup`,
   `/getZone` and persistent-file state.
3. **Before writing**, independently save an exact copy of
   `/mnt/nv/BoseApp-Persistence/1/GroupService.xml` from **each** speaker
   if present, or record that the file is absent. Keep copies outside Git,
   record checksums, and preserve access to the console, SSH and power plugs.
4. Do not run a new stereo job when recovery artifacts exist. Never
   indiscriminately remove or overwrite `.oct-job`, `.oct-bak.*` or
   `.oct-new.*`. If journals disagree, backups are missing, or devices are
   inaccessible, stop and preserve the evidence.

Set values for your test installation (the IDs below are placeholders):

```bash
API="http://127.0.0.1:7778"
LEFT_ID="YOUR_LEFT_DEVICE_ID"
RIGHT_ID="YOUR_RIGHT_DEVICE_ID"
LEFT_IP="YOUR_LEFT_IP"
RIGHT_IP="YOUR_RIGHT_IP"
```

**Read-only API inspection** (repeat as needed):

```bash
curl -fsS "$API/health" | python -m json.tool
curl -fsS "$API/api/stereo-pairs" | python -m json.tool
curl -fsS "$API/api/stereo-pairs/jobs" | python -m json.tool
curl -fsS "http://$LEFT_IP:8090/getGroup"
curl -fsS "http://$RIGHT_IP:8090/getGroup"
curl -fsS "http://$LEFT_IP:8090/getZone"
curl -fsS "http://$RIGHT_IP:8090/getZone"
```

For a configured IPv6 address, enclose its literal in `[ ]` inside the
speaker HTTP URLs. A failed `curl` means **unknown/unreachable**, not
proof of an empty group.

**Read-only on each speaker, in an SSH session:**

```sh
cd /mnt/nv/BoseApp-Persistence/1
ls -la GroupService.xml GroupService.xml.oct-* 2>/dev/null
test -f GroupService.xml && sha256sum GroupService.xml
test -f GroupService.xml.oct-job && cat GroupService.xml.oct-job
```

Do not infer absence solely from the nonzero `ls` exit status of a wildcard.
Check each speaker separately. Original-byte comparison must use the saved
off-device backups, not just an XML status comparison.

## 1. Baseline → create → paired → remove → final

**Baseline (read-only):** both speakers have no current stereo group or
zone, no `oct-*` artifacts, and their persistent file state is known. Note
the count and identity of unrelated existing stereo pairs.

**Create (WRITES files and REBOOTS both speakers):**

```bash
curl -fsS -X POST "$API/api/stereo-pairs" \
  -H "Content-Type: application/json" \
  -d "{\"left_device_id\":\"$LEFT_ID\",\"right_device_id\":\"$RIGHT_ID\",\"name\":\"Stereo hardware test\"}" \
  | python -m json.tool
```

Copy the returned `job_id` into `JOB_ID`. Poll while it is running:

```bash
JOB_ID="PASTE_RETURNED_JOB_ID"
curl -fsS "$API/api/stereo-pairs/jobs/$JOB_ID" | python -m json.tool
curl -fsS "$API/api/stereo-pairs" | python -m json.tool
```

Require `state: succeeded`, `step: done`. Independently inspect **both**
speaker `/getGroup` responses: identical seven-digit group ID, LEFT as
master, exactly two roles (LEFT/RIGHT), both `GROUP_OK`. Verify actual
left/right channel separation and synchronized playback with suitable audio;
an HTTP response cannot establish correct audio separation. Verify the
speaker journals/staging/backups were cleaned after success.

**Remove (WRITES files and REBOOTS both speakers):** copy `group_id` from
the pair list, then run:

```bash
GROUP_ID="PASTE_RETURNED_GROUP_ID"
curl -fsS -X DELETE "$API/api/stereo-pairs/$GROUP_ID" \
  | python -m json.tool
```

Poll the returned removal job as above until `succeeded/done`. **Final
(read-only):** both `/getGroup` and `/getZone` are empty, both persistent
files match their original byte-for-byte copies (or remain absent), no
transaction artifacts remain, and unrelated pairs are unchanged. A
`succeeded` job alone is **not** sufficient for PASS.

## 2. Deliberate power-loss test (optional; HIGH RISK)

Start from a verified baseline and create a new pair as in step 1. Poll its
job. If deliberately testing a lost speaker during reboot, disconnect **only
the dedicated test speaker** when `step: waiting_for_devices` is observed;
record the exact observed phase. Leave it disconnected until the job
finishes or times out. A missed phase is **not tested**, not PASS.

Record `state`, `step`, `error_code`, `failed_device_id` and
`rolled_back`. The disconnected speaker must not be reported as fully
restored. Inspect both journals and surviving speaker state before restoring
power. Reconnect the speaker; **do not** submit another create/remove job
while recovery is required.

## 3. OCT restart and coordinated recovery (optional; HIGH RISK)

To test restart recovery, first retain an actual interrupted transaction's
journals; stop and restart **only** the intended development OCT process with
its **same database**. Record evidence of the process restart. Jobs are
in-memory and will not reappear after restart, but speaker journals should
still be discoverable:

```bash
curl -fsS "$API/api/stereo-pairs" | python -m json.tool
```

Check `recovery_needed_device_ids` and read both speakers' journals via SSH.
Match `job_id`, `operation`, `state`, `had_file` and the required backups.
If they conflict or data is missing, **STOP** rather than deleting anything.
For a verified transaction, request **coordinated API recovery** using either
affected speaker ID (may WRITE and REBOOT both speakers):

```bash
curl -fsS -X POST "$API/api/stereo-pairs/recover/$LEFT_ID" \
  | python -m json.tool
```

Poll the returned recovery job with `GET /api/stereo-pairs/jobs/{job_id}`.
Inspect both speakers independently after recovery: their files must match
the saved pre-job state, firmware state must agree, and all artifacts must be
cleared. If recovery fails, retain evidence and stop; never delete a lone
journal to unblock a new job.

## 4. Post-recovery regression

Only after recovery and independent final-state verification, perform the
complete step 1 sequence again: baseline → create → channel check → remove
→ final. This proves the speakers can be paired normally after recovery.

## Evidence to include in the PR

Document the tested firmware and fork commit, server instance, each job
result, the power-interruption timing, both speakers' `/getGroup` results,
the original-file comparison, leftover cleanup and audible channel
separation. Report the actual server restart separately from a mere status
check. Do not publish private speaker IPs, IDs, backup files, SSH output or
journal contents without explicit review. The manual hardware checklist
belongs in the PR description; this runbook is supporting guidance.
