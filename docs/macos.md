# macOS host setup

The host receiver, recorder, spectrum server, Python controller and SoapySDR module run on
macOS. Build router images and firmware on Linux and copy the matching firmware set and the
unit's own calibration EEPROM to the Mac. The macOS port requires no device firmware changes.

## Build

With Xcode Command Line Tools and Homebrew installed, run from the repository root:

```sh
brew install fftw soapysdr nlohmann-json
make host soapy
```

The Makefiles detect `brew --prefix`; `HOMEBREW_PREFIX` can override it. Both Apple Silicon
and Intel Homebrew paths are supported by the build logic; hardware validation used an M1 Pro.
The Soapy module can also be built with CMake:

```sh
cmake -S host/soapy -B build-soapy
cmake --build build-soapy
```

No system install is needed. Set `SOAPY_SDR_PLUGIN_PATH` to the absolute path of
`host/soapy/build` (or `build-soapy` for CMake), and put `host/bin` on `PATH`:

```sh
export PATH="$PWD/host/bin:$PATH"
export SOAPY_SDR_PLUGIN_PATH="$PWD/host/soapy/build"
```

## Link and controller

The example below uses `en9`, Mac `10.77.0.11/24`, and unit `10.77.0.1`. Use the interface
actually connected to the unit. Set the static address in System Settings or with `ifconfig`.
The temporary MTU setting requires an administrator and may be lost when the link is recreated:

```sh
sudo ifconfig en9 mtu 9216
ifconfig en9
route -n get 10.77.0.1
sysctl kern.ipc.maxsockbuf
```

The receiver requests 8 MiB per socket. This was accepted with `kern.ipc.maxsockbuf=8388608`.
If the local limit is lower, it can be raised temporarily with
`sudo sysctl -w kern.ipc.maxsockbuf=8388608`. The application sets its own receive buffer;
there is no need to change the global UDP receive default. The link must pass UDP payloads
of 8208 bytes. An unprivileged jumbo `ping` can fail at 8192 bytes because of Darwin's raw
socket send limit, even with the interface correctly set to MTU 9216.

Authorize the Mac's SSH public key on the unit, then check that noninteractive login works:

```sh
ssh -o BatchMode=yes root@10.77.0.1 true
```

Configure `~/.config/w1700k/config.json`, or choose another file with `W1700K_CONFIG`:

```json
{
  "unit": {
    "host": "10.77.0.1",
    "user": "root",
    "ssh_key": "~/.ssh/id_ed25519"
  },
  "eeprom": "~/w1700k/eeprom.bin",
  "firmware_dir": "~/w1700k/firmware",
  "export": {
    "interface": "eth1",
    "src_ip": "10.77.0.1",
    "dst_ip": "10.77.0.11",
    "port": 7700,
    "ports": 8
  }
}
```

The firmware directory contains `firmware.json` and its three `.bin` files. `export.interface`
names the interface on the unit. The controller finds the Mac's destination MAC address with
`ifconfig`; `export.dst_mac` can override it. Confirm `host/bin/w1700k status` succeeds.

## Receive

Start the receiver in one terminal:

```sh
host/stream/build/w1700k-stream --bind 10.77.0.11 --source 10.77.0.1 --rx-threads 8 --report 1
```

`--rx-threads` must match `export.ports`: the unit sends from ports 7700 through 7707 into
destination port 7700, and each connected socket receives one source port. This avoids
Darwin's different `SO_REUSEPORT` behavior. Omitting `--source` uses one socket on macOS
and can lose data at 320 Msps.

In another terminal, start the spectrum server and open `http://127.0.0.1:8073/`:

```sh
host/spectrum/build/w1700k-spectrum --controller "host/bin/w1700k serve" --listen 127.0.0.1:8073
```

For SoapySDR, with the environment above:

```sh
SoapySDRUtil --find="driver=w1700k"
SoapySDRUtil --probe="driver=w1700k,band=0"
```

The Python example also needs NumPy and the SoapySDR binding for the same Python interpreter.
With Homebrew Python and its packages, use:

```sh
python3 host/soapy/examples/python/read_iq.py --band 0 --rate 40000000 --seconds 5
```

`sdr-recv` and Soapy read the same POSIX shared-memory ring. On macOS it has no `/dev/shm`
file; use `shm_open` or the supplied C header to map it. Darwin does not expose Linux's
`SO_RXQ_OVFL`; `socket_drops=0` is not a measurement there. Check `lost`, recorder/Soapy
gaps, and changes in `netstat -s -p udp` instead.

## Tests

The native host tests use Homebrew headers and libraries when compiling small test clients:

```sh
export CPATH="$(brew --prefix)/include${CPATH:+:$CPATH}"
export LIBRARY_PATH="$(brew --prefix)/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
python3 -m unittest discover -s tests -p 'test_host_*.py'
python3 -m unittest discover -s tests -p 'test_stream.py'
python3 -m unittest discover -s tests -p 'test_spectrum*.py'
make check-soapy
```

The firmware-build test is skipped without the Linux firmware toolchain. The complete
`make check` suite remains part of Linux validation.
