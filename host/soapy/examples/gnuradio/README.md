# GNU Radio example

Use GNU Radio's **SoapySDR Source** block (from `gr-soapy`) with:

```text
Device arguments: driver=w1700k,band=all
Sample rate:      40e6 | 80e6 | 160e6 | 320e6
Center frequency: e.g. 2442e6
Bandwidth:        20e6 | 40e6 | 80e6 | 160e6 | 320e6
Antenna:          2.4G | 5G | 6G
Gain mode:        automatic / manual
```

`band=all` is important for scripts and GNU Radio: without it SoapySDR completes
`driver=w1700k` with the first enumerated device (2.4G).

The radio has one RX channel and no TX. There is no hardware timestamp; use sample counting
or host time for timing. `w1700k-stream` must be running, and `w1700k` must be on `PATH`
or passed as `cmd=/path/to/w1700k`.

Minimal graph:

```text
SoapySDR Source -> Throttle (optional) -> QT GUI Frequency Sink
```

Do not add a Throttle when the source is already paced by the hardware; it is only useful
when replaying a file.
