from __future__ import annotations

import asyncio


from .harness import make_session


async def test_concurrent_power_on_coalesces_to_one_connect(tv, tmp_path):
    s = make_session(tv, tmp_path)
    connects = 0
    orig = s.connect

    async def counting_connect(**kw):
        nonlocal connects
        connects += 1
        await orig(**kw)

    s.connect = counting_connect
    # 5 concurrent power_on calls must share ONE connect, not five
    results = await asyncio.gather(*[s.power_on() for _ in range(5)])
    assert results == ["Active"] * 5
    assert connects == 1, f"expected 1 coalesced connect, got {connects}"
    await s.disconnect()


async def test_on_and_unblank_share_slot(tv, tmp_path):
    s = make_session(tv, tmp_path)
    connects = 0
    orig = s.connect

    async def counting_connect(**kw):
        nonlocal connects
        connects += 1
        await orig(**kw)

    s.connect = counting_connect
    on, un = await asyncio.gather(s.power_on(), s.unblank())
    assert connects == 1   # both map to the "on" slot
    await s.disconnect()


async def test_sequential_calls_do_not_coalesce(tv, tmp_path):
    s = make_session(tv, tmp_path)
    r1 = await s.power_on()
    r2 = await s.power_on()
    assert r1 == r2 == "Active"   # each completes independently after the last


async def test_busy_flag_tracks_inflight(tv, tmp_path):
    s = make_session(tv, tmp_path)
    assert not s.busy
    task = asyncio.ensure_future(s.power_on())
    await asyncio.sleep(0)   # let it register
    assert s.busy
    await task
    assert not s.busy
    await s.disconnect()
