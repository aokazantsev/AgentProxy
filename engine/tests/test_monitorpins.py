import json
import os

import pytest

from agentproxy.monitorpins import FILE_NAME, MAX_HOST_LENGTH, MAX_PINS, MonitorPins, PinError, PinStorageError


def write_raw(tmp_path, text):
    (tmp_path / FILE_NAME).write_text(text, encoding="utf-8")


def test_missing_file_means_no_pins(tmp_path):
    assert MonitorPins(str(tmp_path)).hosts() == []


def test_pin_and_unpin_keep_order_and_survive_restart(tmp_path):
    pins = MonitorPins(str(tmp_path))
    pins.set("b.example", True)
    pins.set("a.example", True)
    pins.set("b.example", True)
    assert MonitorPins(str(tmp_path)).hosts() == ["b.example", "a.example"]
    assert pins.set("b.example", False) == ["a.example"]
    assert pins.set("never.example", False) == ["a.example"]
    assert not os.path.exists(str(tmp_path / (FILE_NAME + ".tmp")))


@pytest.mark.parametrize("text", ["{broken", "[]", '{"pinned_hosts": "x"}'])
def test_broken_file_reads_as_empty_and_is_replaced_on_save(tmp_path, text):
    write_raw(tmp_path, text)
    pins = MonitorPins(str(tmp_path))
    assert pins.hosts() == []
    assert pins.set("a.example", True) == ["a.example"]
    assert json.loads((tmp_path / FILE_NAME).read_text(encoding="utf-8")) == {"pinned_hosts": ["a.example"]}


def test_invalid_entries_are_skipped(tmp_path):
    write_raw(tmp_path, json.dumps({"pinned_hosts": ["a.example", 5, "", "a.example", "x" * (MAX_HOST_LENGTH + 1), "b.example"]}))
    assert MonitorPins(str(tmp_path)).hosts() == ["a.example", "b.example"]


@pytest.mark.parametrize("host, pinned", [
    ("", True), (None, True), ("x" * (MAX_HOST_LENGTH + 1), True), ("a.example", "yes"), ("bad" + chr(0xDCFF) + ".example", True),
])
def test_invalid_request_is_refused(tmp_path, host, pinned):
    with pytest.raises(PinError):
        MonitorPins(str(tmp_path)).set(host, pinned)
    assert not os.path.exists(str(tmp_path / FILE_NAME))


def test_pin_limit(tmp_path):
    pins = MonitorPins(str(tmp_path))
    for number in range(MAX_PINS):
        pins.set("h{0}.example".format(number), True)
    with pytest.raises(PinError):
        pins.set("one-more.example", True)
    assert len(pins.set("h0.example", True)) == MAX_PINS


def test_unreadable_file_refuses_instead_of_overwriting(tmp_path):
    os.makedirs(str(tmp_path / FILE_NAME))
    pins = MonitorPins(str(tmp_path))
    with pytest.raises(PinStorageError):
        pins.hosts()
    with pytest.raises(PinStorageError):
        pins.set("a.example", True)


def test_file_not_in_utf8_reads_as_empty_and_is_replaced_on_save(tmp_path):
    (tmp_path / FILE_NAME).write_bytes(b'{"pinned_hosts": ["\xff"]}')
    pins = MonitorPins(str(tmp_path))
    assert pins.hosts() == []
    assert pins.set("a.example", True) == ["a.example"]
    assert json.loads((tmp_path / FILE_NAME).read_text(encoding="utf-8")) == {"pinned_hosts": ["a.example"]}


def test_read_keeps_only_first_pins_up_to_limit(tmp_path):
    hosts = ["h{0}.example".format(number) for number in range(MAX_PINS + 5)]
    write_raw(tmp_path, json.dumps({"pinned_hosts": hosts}))
    assert MonitorPins(str(tmp_path)).hosts() == hosts[:MAX_PINS]


def test_failed_save_keeps_previous_file(tmp_path, monkeypatch):
    pins = MonitorPins(str(tmp_path))
    pins.set("a.example", True)

    def refuse(source, target):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PinStorageError):
        pins.set("b.example", True)
    monkeypatch.undo()
    assert pins.hosts() == ["a.example"]
