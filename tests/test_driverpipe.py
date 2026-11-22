"""driverpipe: the driver's stdout is read while the event loop is NOT running (a sync caller that idles),
the frames arrive intact and in order once it runs, send() reaches the driver, stop is clean.
A Python script stands in for the node driver; no browser."""
import asyncio
import json
import os
import sys
import textwrap
import time
import types

import pytest

from deja_browser import driverpipe

pytest.importorskip("playwright")
try:                                                     # playwright <= 1.58
    from playwright._impl._transport import PipeTransport  # noqa: E402
except ImportError:                                      # 1.65 dropped the underscores
    from playwright._impl.transport import PipeTransport  # noqa: E402

FAKE = textwrap.dedent("""
    import json, os, struct, sys
    out, inp = sys.stdout.buffer, sys.stdin.buffer
    def frame(obj):
        b = json.dumps(obj).encode()
        out.write(struct.pack('<I', len(b)) + b); out.flush()
    for i in range(N):                      # far more than any pipe buffer
        frame({"i": i, "pad": "x" * 1000})
    open(MARK, "w").close()                 # reached only if nobody blocked our writes
    while True:                             # echo frames until stdin closes
        h = inp.read(4)
        if len(h) < 4:
            break
        frame({"echo": json.loads(inp.read(struct.unpack('<I', h)[0]))})
""")


@pytest.fixture
def transport_cls(tmp_path):
    """A PipeTransport subclass whose 'driver' is the fake script, installed the way browser._connect does."""
    script = tmp_path / "fake_driver.py"
    mark = tmp_path / "wrote-all"
    script.write_text(f"N = 2000\nMARK = {str(mark)!r}\n" + FAKE)
    base = types.ModuleType("fake_pw_transport")
    base.compute_driver_executable = lambda: (sys.executable, str(script))
    base.get_driver_env = lambda: dict(os.environ)
    base._get_stderr_fileno = lambda: None
    stock = type("PipeTransport", (PipeTransport,), {"__module__": "fake_pw_transport"})
    base.PipeTransport = stock
    cm_mod = types.ModuleType("fake_pw_cm")
    cm_mod.PipeTransport = stock
    sys.modules.update(fake_pw_transport=base, fake_pw_cm=cm_mod)
    cm = type("CM", (), {"__module__": "fake_pw_cm"})()
    try:
        assert driverpipe.install(cm)
        assert driverpipe.install(cm)                    # idempotent
        assert cm_mod.PipeTransport is not stock and issubclass(cm_mod.PipeTransport, stock)
        yield cm_mod.PipeTransport, mark
    finally:
        sys.modules.pop("fake_pw_transport", None)
        sys.modules.pop("fake_pw_cm", None)
        driverpipe._DRAINED.pop("fake_pw_cm", None)


def test_driver_never_blocks_while_the_loop_is_idle(transport_cls, monkeypatch):
    monkeypatch.setattr(driverpipe, "ENABLED", True)
    cls, mark = transport_cls
    loop = asyncio.new_event_loop()
    try:
        t = cls(loop)
        got = []
        t.on_message = got.append
        loop.run_until_complete(t.connect())
        assert isinstance(t._proc, driverpipe.DrainedProc)
        end = time.monotonic() + 20                      # the loop is NOT running here (a sleeping caller)
        while not mark.exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert mark.exists(), "the driver blocked writing 2 MB while the loop was idle"
        task = loop.create_task(t.run())

        async def until(pred, secs=10):
            end = time.monotonic() + secs
            while not pred() and time.monotonic() < end:
                await asyncio.sleep(0.01)
        loop.run_until_complete(until(lambda: len(got) >= 2000))
        assert [m["i"] for m in got] == list(range(2000))
        t.send({"hello": "driver"})
        loop.run_until_complete(until(lambda: len(got) > 2000))
        assert got[-1] == {"echo": {"hello": "driver"}}
        t.request_stop()
        loop.run_until_complete(asyncio.wait_for(t.wait_until_stopped(), 10))
        loop.run_until_complete(asyncio.wait_for(task, 10))
        assert t._proc._popen.poll() is not None
    finally:
        loop.close()


def test_disabled_uses_the_stock_transport(transport_cls, monkeypatch):
    monkeypatch.setattr(driverpipe, "ENABLED", False)
    cls, mark = transport_cls
    loop = asyncio.new_event_loop()
    try:
        t = cls(loop)
        loop.run_until_complete(t.connect())
        assert not isinstance(t._proc, driverpipe.DrainedProc)      # asyncio's own subprocess
        time.sleep(1.5)                                  # loop idle: the stock pipe fills, the driver blocks
        assert not mark.exists(), "control: the stock transport should have blocked the writer"
        t.request_stop()
        t._proc.kill()
        loop.run_until_complete(asyncio.wait_for(t._proc.wait(), 10))
    finally:
        loop.close()


def test_unexpected_layout_is_left_alone():
    mod = types.ModuleType("fake_pw_cm2")
    mod.PipeTransport = type("PipeTransport", (), {"__module__": "nowhere_module"})
    sys.modules["fake_pw_cm2"] = mod
    try:
        cm = type("CM", (), {"__module__": "fake_pw_cm2"})()
        assert driverpipe.install(cm) is False
        assert mod.PipeTransport.__name__ == "PipeTransport"
    finally:
        sys.modules.pop("fake_pw_cm2", None)


def test_real_driver_modules_have_the_expected_layout():
    """The private names install() relies on exist in the installed playwright (and patchright)."""
    from playwright.sync_api import sync_playwright
    cm = sync_playwright()
    mod = driverpipe._builder(cm)
    assert mod is not None, "no module with a PipeTransport: driverpipe would silently stay off"
    base = sys.modules[mod.PipeTransport.__module__]
    for n in ("compute_driver_executable", "get_driver_env", "_get_stderr_fileno"):
        assert hasattr(base, n), n
    exe = base.compute_driver_executable()
    assert isinstance(exe, tuple) and len(exe) == 2
    json.dumps([str(x) for x in exe])
