#!/usr/bin/env python3
"""Minimal SoapySDR receive example for the W1700K.

Run after `w1700k-stream` is serving and the unit is reachable. The module must be installed or
`SOAPY_SDR_PLUGIN_PATH` must point at `host/soapy/build`.

The radio has no hardware timestamps; `timeNs` is 0. CS16 is the native format.
"""
import argparse
import time

import numpy as np
import SoapySDR
from SoapySDR import SOAPY_SDR_CS16, SOAPY_SDR_OVERFLOW, SOAPY_SDR_RX, SOAPY_SDR_TIMEOUT


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--band", default="all", choices=("0", "1", "2", "all"))
    parser.add_argument("--freq", type=float, default=2442e6)
    parser.add_argument("--rate", type=float, default=40e6, choices=(40e6, 80e6, 160e6, 320e6))
    parser.add_argument("--bandwidth", type=float, default=40e6)
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--cmd", default="w1700k", help="path to the host w1700k command")
    args = parser.parse_args()

    device = SoapySDR.Device({"driver": "w1700k", "band": args.band, "cmd": args.cmd})
    device.setSampleRate(SOAPY_SDR_RX, 0, args.rate)
    device.setFrequency(SOAPY_SDR_RX, 0, args.freq)
    device.setBandwidth(SOAPY_SDR_RX, 0, args.bandwidth)
    device.setGainMode(SOAPY_SDR_RX, 0, True)

    stream = device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CS16, [0])
    device.activateStream(stream)
    total = 0
    overflows = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            buffer = np.empty(2 * 65536, dtype=np.int16)
            result = device.readStream(stream, [buffer], 65536, timeoutUs=1000000)
            if result.ret > 0:
                total += result.ret
            elif result.ret == SOAPY_SDR_OVERFLOW:
                overflows += 1
            elif result.ret == SOAPY_SDR_TIMEOUT:
                continue
            else:
                raise RuntimeError("readStream: %d" % result.ret)
    finally:
        device.deactivateStream(stream)
        device.closeStream(stream)

    elapsed = time.monotonic() - started
    print("samples=%d  %.2f Msps  overflows=%d" % (total, total / elapsed / 1e6, overflows))


if __name__ == "__main__":
    main()
