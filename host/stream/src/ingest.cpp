// SPDX-License-Identifier: MIT
#include "ingest.h"
#include "w1700k_posix.h"
#include "w1700k_stream.h"

#include <cstring>
#include <mutex>
#include <stdexcept>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

namespace {

// struct w1700k_sdr_export_header: le32 magic "SDR1", le16 offset, le16 slice_bytes, le64 seq.
constexpr uint32_t kMagic = 0x31524453;
constexpr size_t kHeader = 16;
constexpr unsigned kBatch = 64;
constexpr size_t kDatagram = 9216;
constexpr size_t kRawBytes = kSliceSamples * 4;
constexpr size_t kPackedBytes = kSliceSamples / 8 * 7 * 4;
constexpr size_t kMinSliceBytes = 4096;          // shorter completions are firmware events
constexpr uint64_t kRunGapNs = 1000000000;       // silence that ends a run
constexpr uint64_t kRunJumpBack = 1 << 20;       // slices back: a new run (the driver was reloaded)
constexpr uint64_t kRunJumpForward = uint64_t(1) << 39;  // keeps slot indices below the tag's run field

void atomic_max(std::atomic<uint64_t> &a, uint64_t v)
{
    uint64_t o = a.load(std::memory_order_relaxed);
    while (v > o && !a.compare_exchange_weak(o, v, std::memory_order_acq_rel))
        ;
}

}  // namespace

SampleRing::SampleRing(unsigned capacity_slices)
    : slices_(capacity_slices), words_(size_t(capacity_slices) * kSliceSamples),
      tags_(new std::atomic<uint64_t>[capacity_slices])
{
    if (!capacity_slices || (capacity_slices & (capacity_slices - 1)))
        throw std::runtime_error("ring capacity must be a power of two");
    for (unsigned i = 0; i < capacity_slices; ++i) tags_[i].store(0, std::memory_order_relaxed);
}

void SampleRing::claim(uint64_t index)
{
    tags_[index & (slices_ - 1)].store(0, std::memory_order_relaxed);
    std::atomic_thread_fence(std::memory_order_release);
}

void SampleRing::put(uint64_t run, uint64_t index, const uint32_t *words)
{
    claim(index);
    std::memcpy(&words_[(index & (slices_ - 1)) * kSliceSamples], words, kSliceSamples * 4);
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
}

// Packed slices back to raw words: of each 8 samples, the 8th's 28 bits sit in the spare bits
// [31:30] and [15:14] of the other 7 (o_k = w_k | ((w7 << (14 - 2k)) & 0xc000c000)).
void SampleRing::put_packed(uint64_t run, uint64_t index, const uint32_t *packed)
{
    claim(index);
    uint32_t *out = &words_[(index & (slices_ - 1)) * kSliceSamples];
    for (unsigned g = 0; g < kSliceSamples / 8; ++g, packed += 7, out += 8) {
        uint32_t tail = 0;
        for (unsigned k = 0; k < 7; ++k) {
            out[k] = packed[k] & ~0xc000c000u;
            tail |= (packed[k] & 0xc000c000u) >> (14 - 2 * k);
        }
        out[7] = tail;
    }
    tags_[index & (slices_ - 1)].store(tag_of(run, index), std::memory_order_release);
}

void SampleRing::put_event(uint64_t run, uint64_t index)
{
    tags_[index & (slices_ - 1)].store(event_tag_of(run, index), std::memory_order_release);
}

struct Ingest::Worker {
    int fd = -1;
    std::thread thread;
    std::vector<uint8_t> buffers = std::vector<uint8_t>(kBatch * kDatagram);
    // Written by this worker only, read by stats().
    std::atomic<uint64_t> datagrams{0}, bytes{0}, slices{0}, packed{0}, events{0}, pieces{0}, bad{0}, late{0},
        duplicates{0}, drops{0}, before{0}, spare{0}, stale{0}, stale_lines{0};
};

Ingest::Ingest(SampleRing &ring, const std::string &bind_ip, unsigned port, unsigned threads,
              const std::string &source_ip)
    : ring_(ring), bind_ip_(bind_ip), source_ip_(source_ip), port_(port), threads_(threads)
{
    in_addr check;
    if (inet_pton(AF_INET, bind_ip.c_str(), &check) != 1) throw std::runtime_error("not an IPv4 address: " + bind_ip);
    if (!source_ip.empty() && inet_pton(AF_INET, source_ip.c_str(), &check) != 1)
        throw std::runtime_error("not an IPv4 address: " + source_ip);
    if (!source_ip.empty() && port + threads > 65536)
        throw std::runtime_error("source ports exceed 65535");
}

// Binds the sockets and starts receiving: not before, or the kernel would queue (and then
// drop) what came while nothing read.
void Ingest::start()
{
    if (!workers_.empty()) return;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(uint16_t(port_));
    inet_pton(AF_INET, bind_ip_.c_str(), &address.sin_addr);
    for (unsigned i = 0; i < threads_; ++i) {
        auto w = std::make_unique<Worker>();
#ifdef __APPLE__
        // Darwin's SO_REUSEPORT does not distribute unicast like Linux. Share one socket.
        if (source_ip_.empty() && i) {
            w->fd = w1700k_cloexec(dup(workers_.front()->fd));
            if (w->fd < 0) throw std::runtime_error(std::string("dup UDP socket: ") + std::strerror(errno));
            workers_.push_back(std::move(w));
            continue;
        }
#endif
        int one = 1;
#ifdef __APPLE__
        int rcvbuf = 8 << 20;
#else
        int rcvbuf = 48 << 20;
#endif
        timeval tick{0, 100000};
        w->fd = w1700k_socket(AF_INET, SOCK_DGRAM, 0);
        if (w->fd < 0 || setsockopt(w->fd, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one)) ||
            setsockopt(w->fd, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf)) ||
#ifndef __APPLE__
            setsockopt(w->fd, SOL_SOCKET, SO_RXQ_OVFL, &one, sizeof(one)) ||
#endif
            setsockopt(w->fd, SOL_SOCKET, SO_RCVTIMEO, &tick, sizeof(tick)) ||
            bind(w->fd, reinterpret_cast<sockaddr *>(&address), sizeof(address))) {
            std::string why = std::strerror(errno);
            if (w->fd >= 0) close(w->fd);
            throw std::runtime_error("UDP " + bind_ip_ + ":" + std::to_string(port_) + ": " + why);
        }
        if (!source_ip_.empty()) {
            sockaddr_in source{};
            source.sin_family = AF_INET;
            source.sin_port = htons(uint16_t(port_ + i));
            inet_pton(AF_INET, source_ip_.c_str(), &source.sin_addr);
            if (connect(w->fd, reinterpret_cast<sockaddr *>(&source), sizeof(source))) {
                const std::string why = std::strerror(errno);
                close(w->fd);
                throw std::runtime_error("UDP source " + source_ip_ + ": " + why);
            }
        }
        workers_.push_back(std::move(w));
    }
    for (auto &w : workers_) w->thread = std::thread([this, p = w.get()] { work(*p); });
}

Ingest::~Ingest()
{
    stopping_ = true;
    for (auto &w : workers_) {
        if (w->thread.joinable()) w->thread.join();
        close(w->fd);
    }
}

void Ingest::work(Worker &w)
{
#ifdef __APPLE__
    while (!stopping_.load(std::memory_order_relaxed)) {
        const ssize_t n = recvfrom(w.fd, w.buffers.data(), kDatagram, 0, nullptr, nullptr);
        if (n < 0) continue;
        if (n == ssize_t(kDatagram)) {
            w.bad.fetch_add(1, std::memory_order_relaxed);
            continue;
        }
        const uint64_t t = w1700k_stream_now_ns(), previous = last_ns_.exchange(t, std::memory_order_acq_rel);
        take(w, w.buffers.data(), size_t(n), previous && int64_t(t - previous) > int64_t(kRunGapNs), t);
    }
#else
    mmsghdr msgs[kBatch];
    iovec iov[kBatch];
    char control[kBatch][CMSG_SPACE(sizeof(uint32_t))];
    while (!stopping_.load(std::memory_order_relaxed)) {
        for (unsigned k = 0; k < kBatch; ++k) {
            iov[k] = {&w.buffers[k * kDatagram], kDatagram};
            msgs[k].msg_hdr = msghdr{};
            msgs[k].msg_hdr.msg_iov = &iov[k];
            msgs[k].msg_hdr.msg_iovlen = 1;
            msgs[k].msg_hdr.msg_control = control[k];
            msgs[k].msg_hdr.msg_controllen = sizeof(control[k]);
        }
        int n = recvmmsg(w.fd, msgs, kBatch, MSG_WAITFORONE, nullptr);
        if (n <= 0) continue;
        // A second of silence ends the run: the next slice starts another. Signed: another
        // thread may have stamped a later time between our clock read and the exchange.
        uint64_t t = w1700k_stream_now_ns(), previous = last_ns_.exchange(t, std::memory_order_acq_rel);
        bool fresh = previous && int64_t(t - previous) > int64_t(kRunGapNs);
        for (int k = 0; k < n; ++k) {
            take(w, &w.buffers[k * kDatagram], msgs[k].msg_len, fresh && k == 0, t);
            for (cmsghdr *c = CMSG_FIRSTHDR(&msgs[k].msg_hdr); c; c = CMSG_NXTHDR(&msgs[k].msg_hdr, c))
                if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SO_RXQ_OVFL) {
                    uint32_t drops;
                    std::memcpy(&drops, CMSG_DATA(c), sizeof(drops));
                    w.drops.store(drops, std::memory_order_relaxed);
                }
        }
    }
#endif
}

// Firmware events are filed as event slots rather than dropped: the scheduler steps over them
// without counting a loss, and the samples either side stay contiguous. A completion the unit
// does not send at all (an empty one) cannot be told from a lost datagram and counts as lost.
void Ingest::take(Worker &w, const uint8_t *data, size_t n, bool fresh, uint64_t t)
{
    if (fresh) new_run_.store(true, std::memory_order_release);
    w.datagrams.fetch_add(1, std::memory_order_relaxed);
    w.bytes.fetch_add(n, std::memory_order_relaxed);
    uint32_t magic;
    uint16_t offset, slice_bytes;
    uint64_t seq;
    if (n < kHeader) {
        w.bad.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    std::memcpy(&magic, data, 4);
    std::memcpy(&offset, data + 4, 2);
    std::memcpy(&slice_bytes, data + 6, 2);
    std::memcpy(&seq, data + 8, 8);
    const size_t len = n - kHeader;
    if (magic != kMagic || offset + len > slice_bytes) {
        w.bad.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const bool event = slice_bytes < kMinSliceBytes;
    if (!event && (offset || len != slice_bytes)) {  // the 10G path sends whole slices
        w.pieces.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (!event && slice_bytes != kRawBytes && slice_bytes != kPackedBytes) {
        w.bad.fetch_add(1, std::memory_order_relaxed);
        return;
    }

    // A reset must wait for old writers, including their final newest_ update. Otherwise an
    // old index can advance the new run past samples that have not arrived and create false loss.
    std::shared_lock<std::shared_mutex> writing(run_mutex_);
    uint64_t run = run_.load(std::memory_order_acquire), base = base_.load(std::memory_order_acquire);
    auto jumped = [](uint64_t seq, uint64_t base) {
        return seq < base ? base - seq > kRunJumpBack : seq - base > kRunJumpForward;
    };
    if (!run || jumped(seq, base) || new_run_.load(std::memory_order_acquire)) {
        writing.unlock();
        {
            std::unique_lock<std::shared_mutex> lock(run_mutex_);
            if (run_.load() == run && (new_run_.exchange(false) || !run || jumped(seq, base_.load()))) {
                base_.store(seq);
                newest_.store(0);
                started_ns_.store(t);
                run_.store(run + 1, std::memory_order_release);
            }
        }
        writing.lock();
        run = run_.load(std::memory_order_acquire);
        base = base_.load(std::memory_order_acquire);
    }
    // A completion a little before the run's first was overtaken on the way: late (or, in the
    // run's first second, from before this server joined the stream).
    if (seq < base) {
        (int64_t(t - started_ns_.load()) < int64_t(kRunGapNs) ? w.before : w.late).fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const uint64_t index = seq - base;
    if (index + ring_.capacity_slices() < newest_.load(std::memory_order_relaxed)) {
        w.late.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    const uint64_t held = ring_.tag(index);
    if (held == SampleRing::tag_of(run, index) || held == SampleRing::event_tag_of(run, index)) {
        w.duplicates.fetch_add(1, std::memory_order_relaxed);
        return;
    }
    if (event) {
        ring_.put_event(run, index);
        w.events.fetch_add(1, std::memory_order_relaxed);
    } else if (slice_bytes == kRawBytes) {
        const uint32_t *words = reinterpret_cast<const uint32_t *>(data + kHeader);
        uint64_t spare = 0;
        for (unsigned k = 0; k < kSliceSamples; ++k) spare += (words[k] & 0xc000c000u) != 0;
        if (spare) w.spare.fetch_add(spare, std::memory_order_relaxed);
        ring_.put(run, index, words);
        w.slices.fetch_add(1, std::memory_order_relaxed);
        check(w, run, index);
    } else {
        ring_.put_packed(run, index, reinterpret_cast<const uint32_t *>(data + kHeader));
        w.slices.fetch_add(1, std::memory_order_relaxed);
        w.packed.fetch_add(1, std::memory_order_relaxed);
        check(w, run, index);
    }
    atomic_max(newest_, index + 1);
}

// A slice equal to the one a capture-ring lap earlier is what a reader that overtook the capture
// sends; an 8-sample line equal to the same line a lap earlier (28 bytes packed, 32 raw) is what a
// packer reading a stale D-cache line sends. Noise never repeats; a flat line (all eight samples
// equal, which the radio does output) can, so it is not compared.
void Ingest::check(Worker &w, uint64_t run, uint64_t index)
{
    if (index < kLap || ring_.tag(index - kLap) != SampleRing::tag_of(run, index - kLap)) return;
    const uint32_t *now = ring_.slice(index), *then = ring_.slice(index - kLap);
    uint64_t lines = 0, stale = 0;
    for (unsigned g = 0; g < kSliceSamples; g += 8) {
        bool flat = true;
        for (unsigned k = 1; k < 8 && flat; ++k) flat = now[g + k] == now[g];
        if (flat) continue;
        ++lines;
        stale += std::memcmp(now + g, then + g, 32) == 0;
    }
    if (ring_.tag(index - kLap) != SampleRing::tag_of(run, index - kLap)) return;  // overwritten meanwhile
    if (stale) w.stale_lines.fetch_add(stale, std::memory_order_relaxed);
    if (lines && stale == lines) w.stale.fetch_add(1, std::memory_order_relaxed);
}

Ingest::Position Ingest::position() const
{
    std::shared_lock<std::shared_mutex> lock(run_mutex_);
    return {run_.load(std::memory_order_acquire), newest_.load(std::memory_order_acquire)};
}

void Ingest::restart_run() { new_run_.store(true, std::memory_order_release); }

Ingest::Stats Ingest::stats() const
{
    Stats s;
    for (const auto &w : workers_) {
        s.datagrams += w->datagrams.load(std::memory_order_relaxed);
        s.bytes += w->bytes.load(std::memory_order_relaxed);
        s.slices += w->slices.load(std::memory_order_relaxed);
        s.packed += w->packed.load(std::memory_order_relaxed);
        s.events += w->events.load(std::memory_order_relaxed);
        s.pieces += w->pieces.load(std::memory_order_relaxed);
        s.bad += w->bad.load(std::memory_order_relaxed);
        s.late += w->late.load(std::memory_order_relaxed);
        s.before_start += w->before.load(std::memory_order_relaxed);
        s.duplicates += w->duplicates.load(std::memory_order_relaxed);
        s.socket_drops += w->drops.load(std::memory_order_relaxed);
        s.spare_words += w->spare.load(std::memory_order_relaxed);
        s.stale_slices += w->stale.load(std::memory_order_relaxed);
        s.stale_lines += w->stale_lines.load(std::memory_order_relaxed);
    }
    s.run = run_.load();
    s.newest = newest_.load();
    s.run_started = started_ns_.load() / 1e9;
    s.last_any = last_ns_.load() / 1e9;
    return s;
}
