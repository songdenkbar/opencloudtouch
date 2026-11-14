# Stereo #549 – hardware acceptance runbook

This runbook covers the three **still-open hardware acceptance scenarios**:
power loss during a running job, recovery after OCT restart, and normal operation
after recovery. It also rechecks normal create/remove on the latest build.
All writes/reboots are opt-in; there is no automatic power-cut or server restart.

## Safety first

1. Use **two dedicated SoundTouch 10 devices** with permanent SSH access.
   The source code under test must be the current local fork branch, and the
   running API must be the backend built from that branch, **not an unrelated
   production OCT container**. Check the chosen API's version/health manually.
2. Stop media playback and remove both devices from zones and old stereo pairs.
   Prevent automated grouping or other actors from modifying these speakers.
3. Independently save a **byte-for-byte copy** of each speaker's
   `/mnt/nv/BoseApp-Persistence/1/GroupService.xml`, if present. For a speaker
   where the file truly does not exist, specify `absent`. Keep those backups
   **outside** the repository; do not commit personal device data. The existing
   `test_stereo_hardware_rollback.py` shows how to read and compare them.
4. Record LEFT and RIGHT identifiers and IP addresses. Do not change their
   DHCP leases during the test. Have access to both devices' power plugs,
   the OCT server console, and SSH before starting.
5. **STOP** if a backup is missing, a journal cannot be read, the original
   files differ from the saved baseline unexpectedly, or an operation appears
   inconsistent. Do not manually delete `.oct-job`, `.oct-bak.*` or
   `.oct-new.*` to make another job pass.

From `apps/backend` with the development venv active, define a shell array
for the read-only and opt-in scenario runner (use your **own** IPs and IDs):

```bash
STEREO_TEST=(
  python scripts/test_stereo_hardware_scenarios.py
  --api http://127.0.0.1:7778
  --left-id YOUR_LEFT_ID --left-ip YOUR_LEFT_IP --left-original absent
  --right-id YOUR_RIGHT_ID --right-ip YOUR_RIGHT_IP --right-original absent
)
```

Replace each `absent` with an actual file path if that speaker had an
original file. If using another instance/port, adjust `--api` explicitly.
The runner uses no hidden or hardcoded device addresses.

## 1. Baseline and normal create / remove

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" --phase baseline
PYTHONPATH=src "${STEREO_TEST[@]}" --phase create --execute
PYTHONPATH=src "${STEREO_TEST[@]}" --phase paired
```

The create command prints a job id and watches its steps. Note the group id
from the job or the pair listing. Confirm on **real audio**, using content
with clearly separated channels, that LEFT plays left and RIGHT plays right;
check sync and the Bose app's recognition. The program cannot prove audible
channel separation. Both firmware `/getGroup` responses must report the
same group, LEFT master, correct two roles and `GROUP_OK`; no artifacts remain.

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" --phase remove --group-id GROUP_ID --execute
PYTHONPATH=src "${STEREO_TEST[@]}" --phase final
```

**PASS** only if both original files match the saved bytes, both group and
zone APIs are empty, and neither speaker has any recovery leftovers.

## 2. Power-loss during reboot/wait (critical)

Repeat the read-only baseline first. Only after it passes:

1. With RIGHT's power plug accessible, run
   `PYTHONPATH=src "${STEREO_TEST[@]}" --phase create --execute`.
2. The monitor announces the `rebooting` step. **Physically disconnect
   RIGHT at that point** and keep it offline long enough for the job to
   encounter the timeout. The signal is best effort: the transition can be
   faster than monitoring. Record precisely **when** power was removed.
3. The job is expected to fail; inspect `error_code`, `rolled_back`,
   `failed_device_id`, and both speakers' SSH/journal state. In particular,
   the available LEFT must be restored as far as possible. The disconnected
   RIGHT must **not** be claimed as successfully rolled back; the surviving
   journal(s) must not be silently cleared.
4. Reconnect RIGHT **only after recording the failure and initial state**.
   Run the read-only audit, and do not create another pair yet:

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" --phase after-outage --job-id JOB_ID
```

The intended safety invariant is **no false success and no loss of
recoverability**. If the HTTP endpoint still cannot reach a speaker, note
that separately from SSH reachability. The current runner never deletes
recovery evidence.

## 3. Recovery after OCT restart (critical)

With the failed transaction's journals still in place:

1. Stop **only the intended development OCT server**, using its normal
   shutdown method, and restart the **same instance and database**. This
   step is manual; do not restart the user's production container by accident.
2. Query both devices and OCT again:

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" --phase after-restart
```

The old job will **not** reappear: jobs are in memory by design. Affected
devices should appear under `recovery_needed_device_ids` when reachable.
Attempting a new create/remove should be blocked by leftover protection;
do **not** force another write to demonstrate that unless the preconditions
are fully understood.
3. Inspect the two printed journal states, original backups and reachable
status. If either journal is corrupt, either backup is missing, or the
transaction ids disagree, **STOP** and retain artifacts.
4. For a verified matching two-speaker transaction, request coordinated
recovery via the public endpoint. This is a real-device write/reboot action,
therefore requires **both** opt-ins:

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" \
  --phase recover --recover-id YOUR_LEFT_ID --confirm-recovery --execute
```

This operation may reboot the speakers. Do not retry while a recovery job is
running. If recovery fails, record both journals and the returned error and
stop rather than deleting files.

5. Verify the original state and that **both** speakers are free of leftovers:

```bash
PYTHONPATH=src "${STEREO_TEST[@]}" --phase final
```

## 4. Regression after successful recovery

Only after step 3 passes, repeat **all** commands from step 1:
`baseline → create → paired → remove → final`. This proves that recovery
did not leave the pair database/firmware in a blocked state. Check audio
separation again.

## Evidence for the PR

Record the tested firmware version of both speakers; the exact fork commit;
API port/instance; each job's id, state, error code, failed device and
`rolled_back`; the timing of the power interruption; the observed
`/getGroup` on both devices; and whether journal/backup/staged artifacts
were cleared **only after** a verified end state. Record whether LEFT and
RIGHT channels were audibly distinct. Exclude private IP addresses and device
IDs from the publicly posted evidence unless deliberately approved.

**Do not mark a scenario PASS solely because a job says `succeeded`.**
The physical firmware state, original bytes and leftovers are independently
checked. Also note if a deliberate power cut could not be timed as intended:
that scenario is then **not tested**, rather than passed.
