"""Registry of machine-readable API error codes.

Codes are public contract: they are never renamed or removed after release
(enforced by ``tests/unit/core/test_error_codes.py``). Format:
``^[a-z][a-z0-9_]*$``, unique across the API, prefixed by domain when
feature-specific (for example ``mqtt_invalid_host``). Feature specs add their
constants here.
"""

import re

CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

DEVICE_NOT_FOUND = "device_not_found"
DEVICE_UNREACHABLE = "device_unreachable"
INTERNAL_ERROR = "internal_error"
NOT_FOUND = "not_found"
VALIDATION_ERROR = "validation_error"


# Persistent SoundTouch stereo-pair API errors.
STEREO_SAME_DEVICE = "stereo_same_device"
STEREO_NAME_INVALID = "stereo_name_invalid"
STEREO_MODEL_UNSUPPORTED = "stereo_model_unsupported"
STEREO_DEVICE_INVALID = "stereo_device_invalid"
STEREO_SSH_NOT_AVAILABLE = "stereo_ssh_not_available"
STEREO_SSH_UNREACHABLE = "stereo_ssh_unreachable"
STEREO_DEVICE_IN_ZONE = "stereo_device_in_zone"
STEREO_DEVICE_IN_PAIR = "stereo_device_in_pair"
STEREO_RECOVERY_NEEDED = "stereo_recovery_needed"
STEREO_NOTHING_TO_RECOVER = "stereo_nothing_to_recover"
STEREO_JOB_IN_PROGRESS = "stereo_job_in_progress"
STEREO_JOB_LIMIT_REACHED = "stereo_job_limit_reached"
STEREO_PAIR_NOT_FOUND = "stereo_pair_not_found"
STEREO_GROUP_ID_INVALID = "stereo_group_id_invalid"
STEREO_JOB_NOT_FOUND = "stereo_job_not_found"
STEREO_SSH_FAILED = "stereo_ssh_failed"
STEREO_WRITE_FAILED = "stereo_write_failed"
STEREO_REBOOT_FAILED = "stereo_reboot_failed"
STEREO_VERIFY_FAILED = "stereo_verify_failed"
STEREO_TIMEOUT = "stereo_timeout"
STEREO_ROLLBACK_FAILED = "stereo_rollback_failed"

def validate_code(code: str) -> str:
    """Return *code* unchanged or raise ``ValueError`` if it violates the code format."""
    if not CODE_PATTERN.match(code):
        raise ValueError(
            f"invalid error code {code!r}: must match {CODE_PATTERN.pattern}"
        )
    return code
