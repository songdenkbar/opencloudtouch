"""Opt-in native stereo Marge route tests."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opencloudtouch.marge import native_group_probe


def payload() -> bytes:
    return (b"<group><name>Test</name><masterDeviceId>AABBCCDDEEFF</masterDeviceId>"
            b"<roles><groupRole><deviceId>AABBCCDDEEFF</deviceId><role>LEFT</role></groupRole>"
            b"<groupRole><deviceId>112233445566</deviceId><role>RIGHT</role></groupRole></roles></group>")


def test_create_reuse_and_delete():
    native_group_probe._groups.clear()
    app = FastAPI()
    app.include_router(native_group_probe.router)
    with TestClient(app) as client:
        r = client.post("/streaming/account/1234567/group/", content=payload())
        assert r.status_code == 200 and b'id="' in r.content
        assert client.post("/streaming/account/1234567/group/", content=payload()).content == r.content
        assert client.get("/streaming/account/1234567/device/112233445566/group").content == r.content
        assert client.get("/streaming/account/1234567/device/ABCDEFABCDEF/group").content == b"<group />"
        gid = r.text.split('id="', 1)[1].split('"', 1)[0]
        assert client.delete(f"/streaming/account/1234567/group/{gid}").status_code == 200
        assert client.get("/api/experimental/native-stereo/groups").json() == {"groups": []}
    native_group_probe._groups.clear()


def test_native_remove_requires_exact_roles():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts" / "test_native_stereo.py"
    spec = importlib.util.spec_from_file_location("native_stereo_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    xml = module.group_xml("Test", "AABBCCDDEEFF", "192.168.1.1",
                           "112233445566", "192.168.1.2", group_id="1234567")
    assert module.inspect_existing_group(xml, "AABBCCDDEEFF", "112233445566") == "1234567"
    assert module.inspect_existing_group(b"<group />", "AABBCCDDEEFF", "112233445566") is None
    import pytest
    with pytest.raises(RuntimeError, match="refusing removal"):
        module.inspect_existing_group(xml, "112233445566", "AABBCCDDEEFF")


def test_conflict_and_bad_roles():
    native_group_probe._groups.clear()
    app = FastAPI()
    app.include_router(native_group_probe.router)
    with TestClient(app) as client:
        malformed = payload().replace(b"<role>LEFT</role>", b"<role>RIGHT</role>")
        assert client.post("/streaming/account/1234567/group/", content=malformed).status_code == 422
        assert client.post("/streaming/account/1234567/group/", content=payload()).status_code == 200
        conflict = payload().replace(b"112233445566", b"ABCDEF123456")
        assert client.post("/streaming/account/1234567/group/", content=conflict).status_code == 409
    native_group_probe._groups.clear()
