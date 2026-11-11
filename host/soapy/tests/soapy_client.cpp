// SPDX-License-Identifier: MIT
// Test client for the SoapySDR module: drives it the way an application does and prints
// what it saw as JSON lines, for tests/test_soapy.py to check.
//
//   soapy_client enumerate [DEVICE-ARGS]
//   soapy_client info   DEVICE-ARGS               antennas, frequency range, rates
//   soapy_client tune   DEVICE-ARGS HZ            setFrequency (the module must not throw), then getFrequency
//   soapy_client stream DEVICE-ARGS CS16|CF32 SECONDS
//
// `stream` expects the slice pattern test_soapy.py sends: in slice s, sample n is I = s % 8192,
// Q = n % 2048 (raw 14-bit values): one ramp, continuous across slices.
#include <SoapySDR/Device.hpp>
#include <SoapySDR/Errors.hpp>
#include <SoapySDR/Formats.hpp>

#include <chrono>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

namespace {

void print_list(const char *key, const std::vector<std::string> &items)
{
    std::printf("\"%s\": [", key);
    for (size_t i = 0; i < items.size(); i++) std::printf("%s\"%s\"", i ? ", " : "", items[i].c_str());
    std::printf("]");
}

int stream(SoapySDR::Device *device, const std::string &format, double seconds)
{
    const bool cf32 = format == "CF32";
    SoapySDR::Stream *rx = device->setupStream(SOAPY_SDR_RX, cf32 ? SOAPY_SDR_CF32 : SOAPY_SDR_CS16);
    const int activated = device->activateStream(rx);
    if (activated != 0) {
        std::printf("{\"activate\": %d}\n", activated);
        return 1;
    }

    // 3000 samples is deliberately not a multiple of the slice (2048): reads end inside slices.
    const size_t block = 3000;
    std::vector<int16_t> cs16(2 * block);
    std::vector<float> cf(2 * block);
    void *buffers[1] = {cf32 ? static_cast<void *>(cf.data()) : static_cast<void *>(cs16.data())};

    long long samples = 0, overflows = 0, timeouts = 0, errors = 0, bad_q = 0, jumps = 0, boundaries = 0;
    int last_i = -1, last_q = -1;  // the last sample seen; -1 after a gap, where continuity is not checked
    const auto start = std::chrono::steady_clock::now();
    while (std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count() < seconds) {
        int flags = 0;
        long long time_ns = 0;
        const int n = device->readStream(rx, buffers, block, flags, time_ns, 100000);
        if (n == SOAPY_SDR_OVERFLOW) { overflows++; last_i = last_q = -1; continue; }
        if (n == SOAPY_SDR_TIMEOUT) { timeouts++; continue; }
        if (n < 0) { errors++; break; }
        for (int k = 0; k < n; k++) {
            int i, q;
            if (cf32) {
                i = int(std::lround(cf[2 * k] * 8192));
                q = int(std::lround(cf[2 * k + 1] * 8192));
            } else {
                i = cs16[2 * k];
                q = cs16[2 * k + 1];
            }
            if (last_q >= 0) {
                if (q != (last_q + 1) % 2048) bad_q++;  // the ramp is continuous across slices
                if (q == 0) {                           // a slice boundary: the slice number steps by one
                    if (i == (last_i + 1) % 8192) boundaries++;
                    else jumps++;
                } else if (i != last_i) {
                    bad_q++;                            // I is constant within a slice
                }
            }
            last_i = i;
            last_q = q;
        }
        samples += n;
    }
    device->deactivateStream(rx);
    device->closeStream(rx);
    std::printf("{\"samples\": %lld, \"overflows\": %lld, \"timeouts\": %lld, \"errors\": %lld, \"jumps\": %lld, "
                "\"boundaries\": %lld, \"bad_q\": %lld}\n",
                samples, overflows, timeouts, errors, jumps, boundaries, bad_q);
    return 0;
}

int gains(SoapySDR::Device *device, const std::string &format)
{
    const bool cf32 = format == "CF32";
    auto *rx = device->setupStream(SOAPY_SDR_RX, format);
    if (device->activateStream(rx) != 0) return 1;
    std::vector<int16_t> cs(6000);
    std::vector<float> cf(6000);
    void *buffers[] = {cf32 ? static_cast<void *>(cf.data()) : static_cast<void *>(cs.data())};
    int flags = 0;
    long long ns = 0;
    for (int step = 0; step < 6; ++step) {
        // Cached steps while AGC is on must not interrupt the stream.
        device->setGainMode(SOAPY_SDR_RX, 0, true);
        for (int n = 0; n < 12; ++n) device->readStream(rx, buffers, 3000, flags, ns, 100000);
        device->setGain(SOAPY_SDR_RX, 0, "LNA", 0);
        device->setGain(SOAPY_SDR_RX, 0, "LPF", 5);
        const int cached = device->readStream(rx, buffers, 1000, flags, ns, 100000);
        if (step != 0) {
            device->setGainMode(SOAPY_SDR_RX, 0, false);
            for (int n = 0; n < 12; ++n) device->readStream(rx, buffers, 3000, flags, ns, 100000);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        if (step == 0 || (step < 4 && device->getGainMode(SOAPY_SDR_RX, 0)))
            device->setGainMode(SOAPY_SDR_RX, 0, false);
        else if (step == 1) device->setGain(SOAPY_SDR_RX, 0, "LNA", 2);
        else if (step == 2) device->setGain(SOAPY_SDR_RX, 0, "LPF", 3);
        else if (step == 3) device->setGain(SOAPY_SDR_RX, 0, 8);
        else if (step == 4) device->setGainMode(SOAPY_SDR_RX, 0, true);
        else device->setGainMode(SOAPY_SDR_RX, 0, false);
        const int target = device->getGainMode(SOAPY_SDR_RX, 0) ? 100 :
            int(device->getGain(SOAPY_SDR_RX, 0, "LNA") * 10 + device->getGain(SOAPY_SDR_RX, 0, "LPF"));
        const int first = device->readStream(rx, buffers, 3000, flags, ns, 100000);
        int overflows = first == SOAPY_SDR_OVERFLOW, bad = 0, samples = 0;
        for (int n = 0; n < 12; ++n) {
            const int count = device->readStream(rx, buffers, 3000, flags, ns, 100000);
            if (count == SOAPY_SDR_OVERFLOW) ++overflows;
            if (count <= 0) continue;
            samples += count;
            for (int k = 0; k < count; ++k) {
                const int i = cf32 ? int(std::lround(cf[2 * k] * 8192)) : cs[2 * k];
                if (i != target) ++bad;
            }
        }
        std::printf("{\"step\": %d, \"first\": %d, \"overflows\": %d, \"samples\": %d, "
                    "\"bad_samples\": %d, \"cached_read\": %d}\n", step, first, overflows, samples, bad, cached);
    }
    device->deactivateStream(rx);
    device->closeStream(rx);
    return 0;
}

int settings_concurrent(SoapySDR::Device *device, const std::string &format, bool tuning)
{
    const bool cf32 = format == "CF32";
    if (!tuning) {
        device->setGainMode(SOAPY_SDR_RX, 0, false);
        device->setGain(SOAPY_SDR_RX, 0, "LPF", 5);
    }
    auto *rx = device->setupStream(SOAPY_SDR_RX, format);
    if (device->activateStream(rx) != 0) return 1;
    std::atomic<bool> stop{false};
    std::atomic<int> generation{0}, expected{tuning ? int(device->getFrequency(SOAPY_SDR_RX, 0) / 1e6) : 45};
    long long samples = 0, overflows = 0, errors = 0, bad = 0;
    std::thread reader([&] {
        std::vector<int16_t> cs(6000);
        std::vector<float> cf(6000);
        void *buffers[] = {cf32 ? static_cast<void *>(cf.data()) : static_cast<void *>(cs.data())};
        while (!stop) {
            const int before = generation.load();
            const int target = expected.load();
            int flags = 0;
            long long ns = 0;
            const int count = device->readStream(rx, buffers, 3000, flags, ns, 10000);
            if (count == SOAPY_SDR_OVERFLOW) ++overflows;
            else if (count < 0 && count != SOAPY_SDR_TIMEOUT) ++errors;
            else if (count > 0 && !(before & 1) && before == generation.load()) {
                samples += count;
                for (int k = 0; k < count; ++k) {
                    const int i = cf32 ? int(std::lround(cf[2 * k] * 8192)) : cs[2 * k];
                    if (i != target) ++bad;
                }
            }
        }
    });
    double longest = 0;
    for (int lna : {0, 1, 2, 1, 0, 4}) {
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        ++generation;
        const auto start = std::chrono::steady_clock::now();
        if (tuning) device->setFrequency(SOAPY_SDR_RX, 0, (5250 + lna) * 1e6);
        else device->setGain(SOAPY_SDR_RX, 0, "LNA", lna);
        longest = std::max(longest, std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count());
        expected = tuning ? 5250 + lna : lna * 10 + 5;
        ++generation;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    stop = true;
    reader.join();
    device->deactivateStream(rx);
    device->closeStream(rx);
    std::printf("{\"samples\": %lld, \"overflows\": %lld, \"errors\": %lld, \"bad_samples\": %lld, "
                "\"longest_setter_seconds\": %.6f}\n", samples, overflows, errors, bad, longest);
    return 0;
}

int changes(SoapySDR::Device *device, const std::string &format, bool tuning)
{
    const bool cf32 = format == "CF32";
    auto *rx = device->setupStream(SOAPY_SDR_RX, format);
    if (device->activateStream(rx) != 0) return 1;
    std::vector<int16_t> cs(6000);
    std::vector<float> cf(6000);
    void *buffers[] = {cf32 ? static_cast<void *>(cf.data()) : static_cast<void *>(cs.data())};
    int flags = 0;
    long long ns = 0;
    for (double rate : {40e6, 80e6, 160e6, 320e6, 80e6}) {
        device->readStream(rx, buffers, 1000, flags, ns, 100000);
        std::this_thread::sleep_for(std::chrono::milliseconds(60));
        const auto changed = std::chrono::steady_clock::now();
        if (tuning) device->setFrequency(SOAPY_SDR_RX, 0, 5250e6 + rate);
        else device->setSampleRate(SOAPY_SDR_RX, 0, rate);
        const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - changed).count();
        const double actual = tuning ? device->getFrequency(SOAPY_SDR_RX, 0) : device->getSampleRate(SOAPY_SDR_RX, 0);
        const int first = device->readStream(rx, buffers, 3000, flags, ns, 100000);
        int overflows = first == SOAPY_SDR_OVERFLOW, bad = 0, samples = 0;
        for (int n = 0; n < 12; ++n) {
            const int count = device->readStream(rx, buffers, 3000, flags, ns, 100000);
            if (count == SOAPY_SDR_OVERFLOW) ++overflows;
            if (count <= 0) continue;
            samples += count;
            for (int k = 0; k < count; ++k) {
                const int i = cf32 ? int(std::lround(cf[2 * k] * 8192)) : cs[2 * k];
                if (i != int(actual / 1e6)) ++bad;
            }
        }
        if (!tuning) device->setSampleRate(SOAPY_SDR_RX, 0, actual);
        const int same = device->readStream(rx, buffers, 3000, flags, ns, 100000);
        std::printf("{\"rate\": %.0f, \"change_seconds\": %.6f, \"first\": %d, \"overflows\": %d, "
                    "\"samples\": %d, \"bad_samples\": %d, \"same_rate_read\": %d}\n",
                    actual, seconds, first, overflows, samples, bad, same);
    }
    device->deactivateStream(rx);
    device->closeStream(rx);
    return 0;
}

}  // namespace

int main(int argc, char **argv)
{
    if (argc < 2) return 2;
    const std::string mode = argv[1];
    if (mode == "enumerate") {
        for (const auto &found : SoapySDR::Device::enumerate(argc > 2 ? argv[2] : "driver=w1700k"))
            std::printf("{\"label\": \"%s\", \"band\": \"%s\"}\n", found.at("label").c_str(), found.at("band").c_str());
        return 0;
    }
    if (argc < 3) return 2;
    SoapySDR::Device *device = SoapySDR::Device::make(argv[2]);
    int status = 0;
    if (mode == "info") {
        std::printf("{");
        print_list("antennas", device->listAntennas(SOAPY_SDR_RX, 0));
        const auto range = device->getFrequencyRange(SOAPY_SDR_RX, 0).at(0);
        std::printf(", \"range\": [%.0f, %.0f], \"antenna\": \"%s\", \"rates\": [", range.minimum(), range.maximum(),
                    device->getAntenna(SOAPY_SDR_RX, 0).c_str());
        const auto rates = device->listSampleRates(SOAPY_SDR_RX, 0);
        for (size_t i = 0; i < rates.size(); i++) std::printf("%s%.0f", i ? ", " : "", rates[i]);
        std::printf("]}\n");
    } else if (mode == "tune" && argc == 4) {
        device->setFrequency(SOAPY_SDR_RX, 0, std::atof(argv[3]));
        std::printf("{\"frequency\": %.0f}\n", device->getFrequency(SOAPY_SDR_RX, 0));
    } else if (mode == "stream" && argc == 5) {
        status = stream(device, argv[3], std::atof(argv[4]));
    } else if (mode == "rates" && argc == 4) {
        status = changes(device, argv[3], false);
    } else if (mode == "tunes" && argc == 4) {
        status = changes(device, argv[3], true);
    } else if (mode == "gains" && argc == 4) {
        status = gains(device, argv[3]);
    } else if (mode == "gains-concurrent" && argc == 4) {
        status = settings_concurrent(device, argv[3], false);
    } else if (mode == "tunes-concurrent" && argc == 4) {
        status = settings_concurrent(device, argv[3], true);
    } else {
        status = 2;
    }
    SoapySDR::Device::unmake(device);
    return status;
}
