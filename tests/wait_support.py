"""Shared waits for tests.

A wait ends the moment its condition holds, so a budget only costs time when a
test is about to fail. A loaded runner is slower, not differently behaved: set
LAC_TEST_WAIT_SCALE to multiply every budget here (CI uses 3).

Use these only for something that must happen. A window in which something
must NOT happen keeps its fixed length; scaling it would only slow the test.
"""
import asyncio
import math
import os
import time

SCALE_VARIABLE = "LAC_TEST_WAIT_SCALE"
INTERVAL = 0.005


def wait_scale():
    raw = os.environ.get(SCALE_VARIABLE, "1")
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (1 <= value < math.inf):
        raise ValueError(f"{SCALE_VARIABLE} must be a finite number of at least 1, got {raw!r}")
    return value


def scaled(seconds):
    """A budget for something that must happen, in seconds."""
    return seconds * wait_scale()


def _not_met(timeout, last, describe):
    detail = describe(last) if describe else f"last value: {last!r}"
    return AssertionError(f"Condition not met within {timeout}s x{wait_scale():g}: {detail}")


def poll(read, done=bool, *, timeout=5, interval=INTERVAL, describe=None):
    """Return the first value of read() that satisfies done(), or fail the test.

    The condition is always checked once more after the last sleep.
    """
    deadline = time.monotonic() + scaled(timeout)
    while True:
        value = read()
        if done(value):
            return value
        if time.monotonic() >= deadline:
            raise _not_met(timeout, value, describe)
        time.sleep(interval)


async def poll_async(read, done=bool, *, timeout=5, interval=INTERVAL, describe=None):
    """poll() for a test running on the event loop; read() stays synchronous."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + scaled(timeout)
    while True:
        value = read()
        if done(value):
            return value
        if loop.time() >= deadline:
            raise _not_met(timeout, value, describe)
        await asyncio.sleep(interval)


async def within(awaitable, seconds):
    """asyncio.wait_for with a scaled budget."""
    return await asyncio.wait_for(awaitable, scaled(seconds))
