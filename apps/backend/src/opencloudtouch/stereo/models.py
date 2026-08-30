"""Stereo pair domain models."""

from pydantic import BaseModel, Field


class CreateStereoPairRequest(BaseModel):
    """Request to create a stereo pair."""

    master_device_id: str
    slave_device_id: str
    name: str | None = None


class StereoPairStatus(BaseModel):
    """Status of a stereo pair."""

    group_id: str
    name: str | None = None
    master_device_id: str
    slave_device_id: str
    master_ip: str
    slave_ip: str
    status: str | None = None


class StereoGroupRole(BaseModel):
    """Role of a device within a SoundTouch group."""

    device_id: str
    ip_address: str
    role: str


class StereoGroupStatus(BaseModel):
    """Current SoundTouch group status."""

    group_id: str | None = None
    name: str | None = None
    master_device_id: str | None = None
    sender_ip_address: str | None = None
    status: str | None = None
    roles: list[StereoGroupRole] = Field(default_factory=list)


class StereoVerifyResult(BaseModel):
    """Result of verifying a stereo pair."""

    group_id: str
    verified: bool
    status: str | None = None
    reason: str | None = None
