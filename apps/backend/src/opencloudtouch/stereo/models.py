"""Public request and response schemas for the persistent stereo-pair API."""

from typing import Literal

from pydantic import BaseModel, Field


class CreateStereoPair(BaseModel):
    left_device_id: str
    right_device_id: str
    name: str = Field(min_length=1, max_length=30)


class StereoPair(BaseModel):
    group_id: str
    name: str | None
    status: str | None
    left_device_id: str | None
    right_device_id: str | None
    partial: bool
    inconsistent: bool


class StereoPairList(BaseModel):
    pairs: list[StereoPair]
    unreachable_device_ids: list[str]
    recovery_needed_device_ids: list[str]


class StereoJob(BaseModel):
    job_id: str
    operation: Literal["create", "remove", "recover"]
    group_id: str | None
    name: str | None
    left_device_id: str | None
    right_device_id: str | None
    state: Literal["running", "succeeded", "failed"]
    step: Literal[
        "validating", "backing_up", "writing_files", "rebooting",
        "waiting_for_devices", "verifying", "rolling_back", "done",
    ]
    started_at: str
    finished_at: str | None
    error_code: str | None
    failed_device_id: str | None
    rolled_back: bool | None


class StereoJobList(BaseModel):
    jobs: list[StereoJob]
