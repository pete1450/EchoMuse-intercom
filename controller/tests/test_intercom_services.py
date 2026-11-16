"""
The intercom Home Assistant services (Phase 4): drop_in and hang_up as
ESPHome user-defined services.

The listing is read as source (the temperature-entities pattern): the
gate and the keys are what HA sees, and a function moving cannot leave
a guard passing on nothing. The execution is driven for real against a
bare satellite: _run_intercom_service only needs device_id and
_send_one, so a fake self bound to the real method is the whole
harness. The switchboard decision itself is tested in test_intercom.py.
"""

import asyncio
import re
import types

import pytest

import _sources
import _oww_stub

_oww_stub.install()


@pytest.fixture(autouse=True, scope="module")
def _drop_the_stub():
    # See test_device_speaking.py: a stub left in sys.modules makes other
    # tests' importorskip("openwakeword") succeed against it.
    yield
    _oww_stub.uninstall()


@pytest.fixture
def esphome():
    pytest.importorskip("zeroconf")
    import em_esphome
    return em_esphome


def _services_block() -> str:
    """The ListEntities block that advertises the intercom services."""
    code = _sources.all_code()
    assert code.count("ListEntitiesServicesResponse(") == 2
    # ast.unparse normalises the double quotes to single ones.
    gate = code.index("if self._device_has('intercom'):")
    start = code.rindex("isinstance(msg, api_pb2.ListEntitiesRequest)", 0, gate)
    return code[start:code.index("ListEntitiesDoneResponse()", gate)]


def test_the_service_keys_are_appended():
    code = _sources.all_code()
    assert re.search(r"^DROP_IN_KEY\s*=\s*7$", code, re.M)
    assert re.search(r"^HANG_UP_KEY\s*=\s*8$", code, re.M)


def test_the_services_are_gated_on_the_capability():
    block = _services_block()
    # ast.unparse normalises the double quotes to single ones.
    gate = block.index("if self._device_has('intercom'):")
    assert gate < block.index("name='drop_in'") < block.index("name='hang_up'")


def test_drop_in_takes_the_target_and_hang_up_takes_nothing():
    block = _services_block()
    assert "name='target'" in block
    assert "SERVICE_ARG_TYPE_STRING" in block
    hang_up = block[block.index("name='hang_up'"):]
    assert "args=[]" in hang_up


def test_the_services_return_no_response_data():
    # The reply carries success and an error message only; a service
    # that promised data HA then waits for would hang the automation.
    assert _services_block().count("supports_response=False") == 2


def test_execute_service_is_dispatched():
    code = _sources.all_code()
    start = code.index("isinstance(msg, api_pb2.ExecuteServiceRequest)")
    block = code[start:start + 600]
    assert "_run_intercom_service" in block
    assert "_HANDLED" in block, "the loop must not log the call as unhandled"


class _FakeSatellite:
    def __init__(self, device_id="kitchen"):
        self.device_id = device_id
        self._log_name = device_id
        self.sent = []

    def _send_one(self, msg):
        self.sent.append(msg)


def _msg(key, args=(), call_id=42):
    return types.SimpleNamespace(key=key, args=list(args), call_id=call_id)


def _arg(string):
    return types.SimpleNamespace(string_=string)


def _run(esphome, satellite, msg):
    return asyncio.run(
        esphome.EchoMuseSatellite._run_intercom_service(satellite, msg))


def test_drop_in_reaches_the_switchboard_with_its_target(esphome, monkeypatch):
    import em_intercom
    calls = []

    async def fake_execute(device_id, service, args):
        calls.append((device_id, service, args))
        return (True, "")

    monkeypatch.setattr(em_intercom, "execute_service", fake_execute)
    sat = _FakeSatellite()
    _run(esphome, sat, _msg(esphome.DROP_IN_KEY, [_arg("bedroom")]))
    assert calls == [("kitchen", "drop_in", {"target": "bedroom"})]
    assert len(sat.sent) == 1
    reply = sat.sent[0]
    assert reply.call_id == 42 and reply.success is True
    assert reply.error_message == ""


def test_drop_in_with_no_arguments_sends_an_empty_target(esphome, monkeypatch):
    import em_intercom
    calls = []

    async def fake_execute(device_id, service, args):
        calls.append((device_id, service, args))
        return (True, "")

    monkeypatch.setattr(em_intercom, "execute_service", fake_execute)
    sat = _FakeSatellite()
    _run(esphome, sat, _msg(esphome.DROP_IN_KEY))
    assert calls == [("kitchen", "drop_in", {"target": ""})]


def test_hang_up_reaches_the_switchboard_with_no_arguments(esphome, monkeypatch):
    import em_intercom
    calls = []

    async def fake_execute(device_id, service, args):
        calls.append((device_id, service, args))
        return (True, "")

    monkeypatch.setattr(em_intercom, "execute_service", fake_execute)
    sat = _FakeSatellite()
    _run(esphome, sat, _msg(esphome.HANG_UP_KEY))
    assert calls == [("kitchen", "hang_up", {})]
    assert sat.sent[0].success is True


def test_a_failed_service_reports_its_error_to_ha(esphome, monkeypatch):
    import em_intercom

    async def fake_execute(device_id, service, args):
        return (False, "bedroom is offline")

    monkeypatch.setattr(em_intercom, "execute_service", fake_execute)
    sat = _FakeSatellite()
    _run(esphome, sat, _msg(esphome.DROP_IN_KEY, [_arg("bedroom")]))
    reply = sat.sent[0]
    assert reply.call_id == 42 and reply.success is False
    assert reply.error_message == "bedroom is offline"


def test_an_unknown_service_key_fails_without_calling_the_switchboard(
        esphome, monkeypatch):
    import em_intercom
    calls = []

    async def fake_execute(device_id, service, args):
        calls.append((device_id, service, args))
        return (True, "")

    monkeypatch.setattr(em_intercom, "execute_service", fake_execute)
    sat = _FakeSatellite()
    _run(esphome, sat, _msg(99))
    assert calls == []
    reply = sat.sent[0]
    assert reply.success is False
    assert "99" in reply.error_message
