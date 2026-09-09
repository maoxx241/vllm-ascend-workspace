"""ssh_stream timeout and mux refusal at the analysis skill layer."""
from __future__ import annotations

from types import SimpleNamespace

import conftest  # noqa: F401

import _common as common
import vaws_remote_dev as remote_dev


def _local_endpoint() -> common.SshEndpoint:
    return common.SshEndpoint(host="192.0.2.10", port=22, user="root")


def test_timed_out_stream_raises_timeout_error() -> None:
    original = remote_dev.require_transport

    def fake_require(*args, **kwargs):
        api = dict(original(*args, **kwargs))
        api["run_stream"] = lambda *a, **k: SimpleNamespace(
            returncode=0, timed_out=True, stdout="", stderr=""
        )
        return api

    remote_dev.require_transport = fake_require
    try:
        try:
            common.ssh_stream(_local_endpoint(), "sleep 30", timeout=2, forward_prefix="[t] ")
        except TimeoutError as exc:
            assert "2" in str(exc)
        else:
            raise AssertionError("timed-out stream must raise TimeoutError")
    finally:
        remote_dev.require_transport = original


def test_completed_stream_returns_exit_code() -> None:
    original = remote_dev.require_transport

    def fake_require(*args, **kwargs):
        api = dict(original(*args, **kwargs))
        api["run_stream"] = lambda *a, **k: SimpleNamespace(
            returncode=0, timed_out=False, stdout="", stderr=""
        )
        return api

    remote_dev.require_transport = fake_require
    try:
        assert common.ssh_stream(_local_endpoint(), "true", timeout=None) == 0
    finally:
        remote_dev.require_transport = original


def test_skill_layer_refuses_muxed_stream() -> None:
    """A muxed endpoint passed to run_stream is refused from this skill."""
    api = remote_dev.require_transport()
    endpoint = remote_dev.as_endpoint("192.0.2.10", 22, "root", ssh_mux=True)
    try:
        api["run_stream"](endpoint, "true")
    except api["RemoteExecutionError"] as exc:
        message = str(exc).lower()
        assert "mux" in message or "controlmaster" in message or "stream" in message, exc
    else:
        raise AssertionError("muxed run_stream must raise RemoteExecutionError")


if __name__ == "__main__":
    test_timed_out_stream_raises_timeout_error()
    test_completed_stream_returns_exit_code()
    test_skill_layer_refuses_muxed_stream()
    print("ok")
