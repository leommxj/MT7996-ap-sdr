# SPDX-License-Identifier: MIT
"""Commands with checked results, and moving the unit between its boot stages.

The unit is always in one of these states, named by the prompt it shows:

    stock    vendor U-Boot (`ECNT>`), the first stage; stopped by the boot catcher
    loader   the old SDR U-Boot (`SDR loader>`), kept for the pre-v2 layout
    chainloader  the v2 U-Boot (`XR1710G chainloader>`)
    dumper   the write-free dumper (`dumper>`), loaded by stock over TFTP
    restorer the restorer (`restore>`), loaded by stock over TFTP
    linux    our Linux root shell

Every path to another state goes through stock: a reset or reboot comes back to
stock's countdown, which the catcher stops.
"""
import re
import secrets

STATES = ('stock', 'loader', 'chainloader', 'dumper', 'restorer', 'linux')
COUNTDOWN = re.compile(r'(?i)key to (?:stop|abort|interrupt)')
# U-Boot and shell output that means the command did not do what was asked,
# even when the shell reports success.
FAILURE = re.compile(r'(?i)\b(error|failed|failure|bad hash|not found|unknown command|'
                     r'timeout|exceeded|aborting)\b|Skipping bad block')
POWER_ON_MINUTES = 30
LINUX_READY = re.compile(r'W1700K_BOOT_(READY|DEGRADED)')
HASH_RESULT = re.compile(r'Hash\(es\) for Image \d+ \([^)]*\):\s*(\S+)')


class StepFailed(RuntimeError):
    """A command returned failure or unexpected output; nothing after it ran."""


def command(console, line, expect=None, limit=60.0, heartbeat=None, about=None,
            strict=True):
    """Run `line`, require exit status 0, and return its output.

    The status is echoed after the command with a token unique to this call, so
    output from an earlier command cannot satisfy it. With `strict`, failure words
    in the output fail the step too (U-Boot reports some errors with status 0).
    `expect`, a regex, must match the output.
    """
    token = f'W1700K_RC_{secrets.token_hex(4)}='
    text = console.must_run(f'{line}; echo {token}$?', limit=limit, heartbeat=heartbeat,
                            about=about)
    status = re.search(re.escape(token) + r'(\d+)\s*$', text, re.M)
    if not status or status.group(1) != '0':
        raise StepFailed(f'{line!r} failed (status '
                         f'{status.group(1) if status else "unknown"}):\n{text[-600:]}')
    body = text[:status.start()]
    if strict and FAILURE.search(body.split('\n', 1)[-1]):
        raise StepFailed(f'{line!r} reported a failure:\n{text[-600:]}')
    if expect is not None and not re.search(expect, body):
        raise StepFailed(f'{line!r} did not print {expect!r}:\n{text[-600:]}')
    return body


def verify_fit(console, address, algorithm='sha1'):
    """U-Boot checks the hash of every image in the FIT at `address`.

    Stock U-Boot knows SHA-1 only; FITs read by our loader carry SHA-256.
    """
    text = command(console, f'iminfo {address:#x}', limit=90)
    results = HASH_RESULT.findall(text)
    if not results or any(result != algorithm + '+' for result in results):
        raise StepFailed(f'the FIT at {address:#x} did not verify:\n{text[-600:]}')


def identify(console, log, settle=12.0):
    """Name the console state, transmitting nothing but one Ctrl-C."""
    console.wake()
    start = console.mark()
    console.wait(lambda: any(console.at_prompt(name) for name in STATES), settle,
                 'a known prompt')
    for name in STATES:
        if console.at_prompt(name):
            log.event('state', state=name)
            return name
    state = 'countdown' if COUNTDOWN.search(console.view(start)) else 'unknown'
    log.event('state', state=state)
    return state


def wait_for_power_on(console, minutes=POWER_ON_MINUTES):
    """Stop stock U-Boot's countdown on the next boot, which the operator starts."""
    console.arm()
    print(f'  POWER THE UNIT ON (or power-cycle it) NOW; waiting up to {minutes} min')
    console.wait_for_catch(minutes * 60)
    console.expect_prompt('stock', timeout=15)


def to_stock(console, log, state, fresh=False):
    """Reach the stock prompt from any state. With `fresh`, stock's own boot is
    always seen in this run (a unit already at its prompt is reset), for what it
    prints about the NAND (`bmt pool size: N`)."""
    if state == 'stock' and not fresh:
        return
    if state in STATES:
        console.arm()
        # `reboot -f` so a wedged userspace cannot stop it.
        console.send_async('reboot -f' if state == 'linux' else 'reset')
        console.wait_for_catch(180)
        console.expect_prompt('stock', timeout=15)
    else:
        wait_for_power_on(console)
    log.event('transition', frm=state, to='stock')


def to_installed_loader(console, log, state):
    """Reach the prompt of the old loader installed in NAND (via stock's bootcmd)."""
    if state == 'loader':
        return
    to_stock(console, log, state)
    console.send_async('run bootcmd')
    console.interrupt_autoboot('loader', limit=60)
    console.expect_prompt('loader', timeout=10)
    log.event('transition', frm='stock', to='loader')


def to_chainloader(console, log, state):
    """Reach the v2 chainloader prompt via stock's saved bootcmd."""
    if state == 'chainloader':
        return
    to_stock(console, log, state)
    console.send_async('run bootcmd')
    console.interrupt_autoboot('chainloader', limit=90)
    console.expect_prompt('chainloader', timeout=15)
    log.event('transition', frm='stock', to='chainloader')


def boot_linux(console, log, command_line='run bootcmd', limit=420):
    """Boot Linux from the loader prompt and wait for the startup result."""
    start = console.mark()
    console.send_async(command_line)

    def ready():
        return LINUX_READY.search(console.view(start)) and console.at_prompt('linux')

    if not console.wait(ready, limit, 'Linux startup'):
        raise StepFailed(f'Linux did not finish starting within {limit} s:\n'
                         f'{console.view(start)[-800:]}')
    console.expect_prompt('linux', timeout=10)
    text = console.view(start)
    log.text('linux-boot.txt', text)
    status = LINUX_READY.search(text).group(0)
    log.event('linux', status=status)
    return status
