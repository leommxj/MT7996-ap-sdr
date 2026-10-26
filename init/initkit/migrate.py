# SPDX-License-Identifier: MIT
"""Move an installed unit from the old 48 MiB layout to the v2 439 MiB layout.

This is the only path that writes the chainloader and creates the v2 UBI. It
runs at the stock U-Boot prompt, writes the chainloader through stock, then
drives the chainloader console to erase the new `ubi` partition and create the
six volumes. Nothing here uses the old SDR loader or its BMT write path.
"""
from pathlib import Path
import sys

from . import config, console as unit, layout, nand as parts, stock, steps

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'platform'))
import fdt  # noqa: E402
from .console import StepFailed, command, verify_fit
from .session import open_session

CHAIN_EXTENT = 0x40000          # enough for the ~204 KiB FIT, whole erase blocks
CHAIN_SLOT = 0x100000
READBACK = layout.READBACK
STAGE = layout.STOCK_STAGE


def padded_chainloader(session):
    data = config.CHAINLOADER.read_bytes()
    if len(data) > CHAIN_EXTENT:
        raise StepFailed(f'the chainloader is {len(data)} bytes; the stock staging '
                         f'extent is {CHAIN_EXTENT} bytes')
    path = session.log.dir / 'w1700k-chainloader.bin'
    path.write_bytes(data + b'\xff' * (CHAIN_EXTENT - len(data)))
    return path


def save_chainloader_bootcmd(session):
    console = session.console
    environment = stock.environment(console)
    current = environment.get('bootcmd', '')
    if "'" in current:
        raise StepFailed(f'cannot preserve a bootcmd containing a quote: {current!r}')
    if 'bootcmd_orig' not in environment and current:
        command(console, f"setenv bootcmd_orig '{current}'", limit=20)
    bootcmd = (f'flash read 0x600000 {CHAIN_SLOT:#x} 0x89000000; '
               f'bootm 0x89000000')
    command(console, f"setenv bootcmd '{bootcmd}'", limit=20)
    command(console, 'saveenv', limit=120)
    saved = stock.environment(console).get('bootcmd')
    if saved != bootcmd:
        raise StepFailed(f'stock saved bootcmd {saved!r}, expected {bootcmd!r}')
    session.log.event('chainloader_bootcmd', bootcmd=saved,
                      bootcmd_orig=stock.environment(console).get('bootcmd_orig'))


def write_chainloader(session):
    console = session.console
    console.expect_prompt('stock', timeout=10)
    image = padded_chainloader(session)
    stock.stage(console, session.log, session.settings, image, session.served)
    verify_fit(console, STAGE)
    for line in (f'flash erase 0x600000 {CHAIN_SLOT:#x}',
                 f'flash write 0x600000 {CHAIN_EXTENT:#x} {STAGE:#x}',
                 f'flash read 0x600000 {CHAIN_EXTENT:#x} {READBACK:#x}',
                 f'cmp.b {STAGE:#x} {READBACK:#x} {CHAIN_EXTENT:#x}'):
        command(console, line, limit=300, heartbeat=30)
    session.log.event('chainloader_written', extent=CHAIN_EXTENT)
    save_chainloader_bootcmd(session)


def boot_chainloader(session):
    console, log = session.console, session.log
    start = console.mark()
    console.send_async('bootm 0x89000000')
    # Do not send keys while stock is still decompressing the FIT: the console
    # would swallow them and abort the boot. Wait for the chainloader's banner.
    if not console.wait(lambda: ('U-Boot 2026.07' in console.view(start) or
                                 'Boot Menu' in console.view(start)),
                        25, 'chainloader banner'):
        raise StepFailed('stock did not start the chainloader from RAM:\n'
                         + console.view(start)[-800:])
    for _ in range(150):
        if console.at_prompt('chainloader'):
            break
        # The menu accepts ESC; the boot countdown accepts any key. Send ESC so
        # a visible menu is exited instead of selecting its default entry.
        console._transmit(b'\x1b', 'chainloader menu exit')
        console.wait(lambda: console.at_prompt('chainloader'), 0.2, 'chainloader prompt')
    console.expect_prompt('chainloader', timeout=10)
    log.event('transition', frm='stock', to='chainloader')


def create_volumes(session, nand):
    console = session.console
    for volume_id, (name, kind, lebs) in enumerate(partitions_volumes(nand)):
        size = config.partitions.ENV_BYTES if name.startswith('ubootenv') else \
            (lebs * nand.leb if kind == 'static' else '-')
        size_arg = hex(size) if isinstance(size, int) else size
        command(console, f'ubi create {name} {size_arg} '
                         f'{"s" if kind == "static" else "d"} {volume_id}',
                expect='Creating', limit=300)


def partitions_volumes(nand):
    from .config import partitions
    return partitions.v2_volumes(nand)


def format_ubi(session):
    console, log = session.console, session.log
    listing = console.must_run('mtd list', limit=30)
    log.text('mtd-list-v2.txt', listing)
    nand = parts.identify(listing)
    print(f'migrate: NAND {nand.name}; erasing the 439 MiB ubi partition')
    console.expect_prompt('chainloader', timeout=10)
    # Erase in 16 MiB commands: bounded output and a restartable operation if
    # the console drops, instead of one 439 MiB command.
    for offset in range(0, 0x1b700000, 0x1000000):
        command(console, f'mtd erase ubi {offset:#x} '
                         f'{min(0x1000000, 0x1b700000 - offset):#x}',
                limit=300, heartbeat=30)
    command(console, 'ubi part.rw ubi', expect='read-write', strict=False, limit=900)
    create_volumes(session, nand)
    command(console, 'setenv bootslot a', limit=20)
    command(console, 'setenv upgrade_available 0', limit=20)
    command(console, 'setenv bootlimit 0', limit=20)
    command(console, 'setenv bootcount 0', limit=20)
    command(console, 'saveenv', limit=120)
    steps.loader_network(session)
    return nand


def write_everything(session, nand):
    steps.write_volume(session, 'fit_a', config.SYSTEM_FIT, fit=True)
    steps.write_volume(session, 'fit_b', config.SYSTEM_FIT, fit=True)
    steps.write_volume(session, 'recovery', config.RECOVERY_FIT, fit=True)
    data = config.IMAGES / f'data-{nand.name.lower()}.ubifs'
    steps.write_volume(session, 'data', data)
    command(session.console, 'setenv bootslot a', limit=20)
    command(session.console, 'setenv upgrade_available 0', limit=20)
    command(session.console, 'setenv bootlimit 0', limit=20)
    command(session.console, 'setenv bootcount 0', limit=20)
    command(session.console, 'saveenv', limit=120)


def boot_installed(session):
    status = unit.boot_linux(session.console, session.log, 'run boot_a')
    command(session.console, '/usr/sbin/verify-storage',
            expect='W1700K_STORAGE_READY', limit=120)
    return status


def fitted_unit():
    tree = fdt.nodes(config.SYSTEM_FIT.read_bytes())
    return fdt.string(tree['/configurations/conf-sdr']['sdr,unit'])


def running_unit(console):
    text = console.must_run('cat /etc/w1700k/unit', limit=10)
    lines = [line.strip() for line in text.replace('\r', '').splitlines()
             if line.strip() and not line.strip().endswith('#')]
    return lines[-1] if lines else ''


def run(settings, expect_serial=None, force=False):
    config.require_images(config.CHAINLOADER, config.SYSTEM_FIT, config.RECOVERY_FIT,
                          *config.DATA_IMAGES)
    image_unit = fitted_unit()
    if expect_serial and image_unit != expect_serial:
        raise StepFailed(f'the FIT is for {image_unit}, not {expect_serial}; nothing was written')
    with open_session(settings, 'migrate', write=True) as session:
        console, log = session.console, session.log
        state = unit.identify(console, log)
        print(f'migrate: the unit is at: {state}')
        if state == 'linux':
            actual = running_unit(console)
            print(f'migrate: running unit {actual}, FIT unit {image_unit}')
            if actual != image_unit:
                raise StepFailed(f'the unit is {actual}, but the FIT is for {image_unit}; '
                                 'nothing was written')
        elif expect_serial:
            raise StepFailed('the serial can only be checked from the running SDR '
                             'Linux; boot the old SDR first or omit --expect-serial')
        unit.to_stock(console, log, state, fresh=True)
        environment = stock.environment(console)
        log.text('stock-environment-v2.txt', '\n'.join(
            f'{key}={value}' for key, value in sorted(environment.items())))
        if 'bootcmd_orig' not in environment:
            print('migrate: WARNING: stock bootcmd_orig is absent; saving current bootcmd')
        extent = layout.bootcmd_extent(environment.get('bootcmd'))
        if extent == 0x100000 and not force:
            raise StepFailed('stock already boots the v2 chainloader; use --force to reinstall')
        print('migrate: writing the 1 MiB chainloader slot')
        write_chainloader(session)
        print('migrate: booting the chainloader from RAM')
        boot_chainloader(session)
        nand = format_ubi(session)
        if expect_serial:
            # The chainloader has no serial helper; the unit name is verified by
            # the FIT's sdr,unit and the rootfs /etc/w1700k/unit after boot.
            print(f'migrate: expecting unit {expect_serial} (FIT attestation checked on boot)')
        print('migrate: writing FIT A/B, recovery and data')
        write_everything(session, nand)
        print('migrate: booting the installed system')
        status = boot_installed(session)
    print(f'migrate: done; {status}')
    return 0
