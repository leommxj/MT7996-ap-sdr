// SPDX-License-Identifier: MIT
//
// Control path of the W1700K module: running the host's `w1700k` command and keeping the radio
// state it reports.
//
// The radio itself is controlled over ssh by the host-side `w1700k` program (host/w1700k in the w1700k-sdr project); its
// state lives on the unit. One `w1700k serve` process carries requests and state replies for the
// lifetime of a device. Nothing here touches the stream; that is Streaming.cpp.
#pragma once

#include <chrono>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace w1700k {

// The three RF bands; the SoapySDR "antenna" of this module is the band.
constexpr int kBandCount = 3;
extern const char *const kBandNames[kBandCount];  // "2.4G", "5G", "6G"

// SoapySDR passes booleans as text: "true" and "1" (and "yes", "on") are true.
bool is_true(const std::string &text);

// What the unit last reported. The defaults are the values of a freshly loaded unit and are only
// used until the first reply arrives.
struct RadioState {
    int band = 1;                  // the band the capture is switched to
    double lo_hz = 5250e6;         // that band's LO, the centre of the stream
    double sample_rate = 320e6;    // samples per second of the stream
    double capture_rate = 320e6;   // 640e6 for "640/2" (every other sample streamed), else the sample rate
    double filter_hz = 320e6;      // the analog filter's width
    bool streaming = false;        // the unit is exporting IQ
    bool agc = true;
    int lna = -1, lpf = -1;        // forced gain steps; -1: AGC is on and they are not reported
    // The span each band's synthesizer locks over on this unit (it differs from unit to unit by
    // tens of MHz). The defaults are one unit's, until the unit reports its own.
    double lo_span_hz[kBandCount][2] = {{2080.7e6, 2748.0e6}, {4646.6e6, 7509.0e6}, {4707.2e6, 7576.2e6}};
};

// Updates `state` from the JSON a `w1700k` command printed. A field the reply lacks keeps its
// value (the gain steps are the exception: they are reported only while AGC is off, and a reply
// that carries the `rf` block without them means AGC is on). Throws std::runtime_error, leaving
// `state` as it was, if the text is not a reply, reports an error or has a malformed field.
void parse_reply(const std::string &json_text, RadioState &state);

// Runs `argv` (argv[0] is looked up in PATH unless it contains a slash). stdout and stderr are
// captured separately, stdin is /dev/null. A process still running after `timeout` is killed.
// Returns the exit status, or -1 if it could not be started, was killed or died on a signal.
int run_process(const std::vector<std::string> &argv, std::chrono::seconds timeout, std::string &out,
                std::string &err);

class CommandSession;

// Serializes control requests and holds the state they report.
class Controller {
public:
    explicit Controller(std::string command, bool persistent = true,
                        std::chrono::milliseconds timeout = std::chrono::seconds(30));
    ~Controller();

    // Sends the equivalent of `w1700k <args>` and applies its reply. A broken session is closed;
    // the next call reconnects. An operation whose outcome is unknown is never replayed.
    void run(const std::vector<std::string> &args);

    // Reads the unit's current state (`w1700k status --rf`).
    void refresh();

    // A copy of the last known state. Never waits for a command that is running.
    RadioState state() const;

    const std::string &command() const { return command_; }

private:
    const std::string command_;
    const bool persistent_;
    const std::chrono::milliseconds timeout_;
    std::unique_ptr<CommandSession> session_;
    std::mutex run_mutex_;           // one command at a time
    mutable std::mutex state_mutex_; // guards state_ only, so readers do not wait for a command
    RadioState state_;
};

}  // namespace w1700k
