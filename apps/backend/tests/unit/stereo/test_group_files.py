"""Contract tests for the SoundTouch stereo XML fixture from issue #549."""

import pytest

from opencloudtouch.stereo.group_files import (
    build_group_xml,
    parse_group_xml,
    validate_name,
)

LEFT = "AABBCC112233"
RIGHT = "AABBCC445566"


@pytest.mark.parametrize("right_file", [False, True])
def test_file_round_trip(right_file: bool) -> None:
    xml = build_group_xml(
        "1234567",
        "Living & Dining",
        LEFT,
        "192.0.2.10",
        RIGHT,
        "192.0.2.11",
        right_file=right_file,
    )
    group = parse_group_xml(xml)
    assert group is not None
    assert group.group_id == "1234567"
    assert group.name == "Living & Dining"
    assert group.master_device_id == LEFT
    assert [(r.device_id, r.role) for r in group.roles] == [
        (LEFT, "LEFT"),
        (RIGHT, "RIGHT"),
    ]
    assert group.status == ("GROUP_OK" if right_file else None)
    assert xml.count("<status>GROUP_OK</status>") == int(right_file)
    assert "<name>Living &amp; Dining</name>" in xml
    assert "<senderIPAddress>192.0.2.10</senderIPAddress>" in xml


@pytest.mark.parametrize(
    "name",
    [
        "",
        " " * 3,
        "x" * 31,
        "Bad\x00name",
        "Bad\nname",
        "\nStereo",
        "Stereo\n",
        "Stereo\t",
    ],
)
def test_invalid_name(name: str) -> None:
    with pytest.raises(ValueError):
        validate_name(name)


@pytest.mark.parametrize(
    ("left", "right_ip", "group_id"),
    [
        ("INVALID", "192.0.2.11", "1234567"),
        (LEFT, "192.0.2.11; reboot", "1234567"),
        (LEFT, "192.0.2.11", "id&bad"),
    ],
)
def test_invalid_fields(left: str, right_ip: str, group_id: str) -> None:
    with pytest.raises(ValueError):
        build_group_xml(
            group_id,
            "Living Room",
            left,
            "192.0.2.10",
            RIGHT,
            right_ip,
            right_file=False,
        )


def test_rejects_same_device() -> None:
    with pytest.raises(ValueError):
        build_group_xml(
            "1234567",
            "Living Room",
            LEFT,
            "192.0.2.10",
            LEFT,
            "192.0.2.11",
            right_file=True,
        )


@pytest.mark.parametrize(
    "xml",
    [
        "<group />",
        "<group><broken>",
        '<!DOCTYPE group [<!ENTITY x "bad">]><group id="123"><masterDeviceId>&x;</masterDeviceId></group>',
    ],
)
def test_unparseable_or_unsafe_xml_returns_none(xml: str) -> None:
    assert parse_group_xml(xml) is None


def test_group_models_use_the_device_client_contract():
    """No duplicate GroupInfo/GroupRole types between client and XML parser."""
    from opencloudtouch.devices.client import (
        DeviceClient,
        GroupInfo as ClientGroupInfo,
        GroupRole as ClientGroupRole,
    )
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole

    assert GroupInfo is ClientGroupInfo
    assert GroupRole is ClientGroupRole
    assert "get_group" in DeviceClient.__abstractmethods__
