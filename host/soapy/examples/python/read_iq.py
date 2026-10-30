#!/usr/bin/env python3
"""Minimal SoapySDR receive example for the W1700K.

Run after `w1700k-stream` is serving and the unit is reachable. The module is found in the source
tree's build directory, a test bundle's bin directory, or via --module / W1700K_SOAPY_MODULE.

The radio has no hardware timestamps; `timeNs` is 0. CS16 is the native format.
"""
import argparse
import os
import time
from pathlib import Path

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_OVERFLOW, SOAPY_SDR_RX, SOAPY_SDR_TIMEOUT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--band", default="0", choices=("0", "1", "2", "all"))
    parser.add_argument("--antenna", choices=("2.4G", "5G", "6G"), help="required with --band all")
    parser.add_argument("--freq", type=float, default=2442e6)
    parser.add_argument("--rate", type=float, default=40e6, choices=(40e6, 80e6, 160e6, 320e6))
    parser.add_argument("--bandwidth", type=float, default=40e6)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--cmd", default="w1700k", help="path to the host w1700k command")
    parser.add_argument("--module", default=os.environ.get("W1700K_SOAPY_MODULE"),
                        help="path to libw1700kSupport.so (default: $W1700K_SOAPY_MODULE or build/bin in the tree)")
    args = parser.parse_args()
    if args.band == "all" and not args.antenna:
        parser.error("--band all requires --antenna")
    if args.band != "all" and args.antenna and args.antenna != ("2.4G", "5G", "6G")[int(args.band)]:
        parser.error("--antenna must match --band")

    module = args.module
    if not module:
        roots = Path(__file__).resolve().parents[1:3]
        for candidate in (root / folder / "libw1700kSupport.so" for root in roots for folder in ("build", "bin")):
            if candidate.is_file():
                module = str(candidate)
                break
    if not module:
        raise SystemExit("set --module or W1700K_SOAPY_MODULE to libw1700kSupport.so")

    # Disable SoapySDR's automatic module loading. Otherwise a host with SoapyUHD
    # installed can spend minutes scanning UHD networks before this driver is tried.
    manager = SoapySDR.ModuleManager(False)
    SoapySDR.loadModule(module)

    # The Radioconda SWIG binding accepts SoapySDRKwargs reliably; a plain dict
    # can reach Device::make() as no-match on this version.
    device_args = SoapySDR.SoapySDRKwargs()
    device_args["driver"] = "w1700k"
    device_args["band"] = args.band
    device_args["cmd"] = args.cmd
    device = SoapySDR.Device(device_args)
    stream = None
    active = False
    total = 0
    overflows = 0
    try:
        if args.antenna:
            device.setAntenna(SOAPY_SDR_RX, 0, args.antenna)
        device.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
        if not any(r.minimum() <= args.freq <= r.maximum() for r in device.getFrequencyRange(SOAPY_SDR_RX, 0)):
            raise ValueError("--freq is outside the selected band's tuning range")
        device.setFrequency(SOAPY_SDR_RX, 0, args.freq)
        device.setBandwidth(SOAPY_SDR_RX, 0, args.bandwidth)
        device.setGainMode(SOAPY_SDR_RX, 0, True)
        print("antenna=%s  frequency=%.6f MHz  rate=%.0f Msps" %
              (device.getAntenna(SOAPY_SDR_RX, 0), device.getFrequency(SOAPY_SDR_RX, 0) / 1e6,
               device.getSampleRate(SOAPY_SDR_RX, 0) / 1e6), flush=True)

        stream = device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0])
        result = device.activateStream(stream)
        if result != 0:
            raise RuntimeError("activateStream: %d" % result)
        active = True
        buffer = np.empty(2 * 65536, dtype=np.int16)
        started = time.monotonic()
        while time.monotonic() - started < args.seconds:
            result = device.readStream(stream, [buffer], 65536, timeoutUs=1000000)
            if result.ret > 0:
                total += result.ret
            elif result.ret == SOAPY_SDR_OVERFLOW:
                overflows += 1
            elif result.ret == SOAPY_SDR_TIMEOUT:
                continue
            else:
                raise RuntimeError("readStream: %d" % result.ret)
        elapsed = time.monotonic() - started
    finally:
        if active:
            device.deactivateStream(stream)
        if stream is not None:
            device.closeStream(stream)
        device.close()

    print("samples=%d  %.2f Msps  overflows=%d" % (total, total / elapsed / 1e6, overflows))


if __name__ == "__main__":
    main()
