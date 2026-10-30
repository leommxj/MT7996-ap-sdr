// SPDX-License-Identifier: MIT
#include "ingest.h"

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sys/socket.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    SampleRing ring(16384);
    Ingest ingest(ring, "127.0.0.1", std::atoi(argv[1]), 8);
    ingest.start();
    std::atomic<bool> stop{false};
    std::thread sender([&] {
        int sockets[8];
        for (auto &fd : sockets) fd = socket(AF_INET, SOCK_DGRAM, 0);
        sockaddr_in dst{};
        dst.sin_family = AF_INET;
        dst.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        dst.sin_port = htons(std::atoi(argv[1]));
        uint8_t packet[16 + 8192]{};
        const uint32_t magic = 0x31524453;
        const uint16_t bytes = 8192;
        std::memcpy(packet, &magic, 4);
        std::memcpy(packet + 6, &bytes, 2);
        for (uint64_t seq = 0; !stop; ++seq) {
            std::memcpy(packet + 8, &seq, 8);
            std::memcpy(packet + 16, &seq, 8);
            sendto(sockets[seq % 8], packet, sizeof(packet), 0, reinterpret_cast<sockaddr *>(&dst), sizeof(dst));
            if (seq % 64 == 63) std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
        for (auto fd : sockets) close(fd);
    });
    int checked = 0, corrupt = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(25);
    while (checked < 100 && std::chrono::steady_clock::now() < deadline) {
        const auto old = ingest.position();
        if (old.newest < 8192) {
            std::this_thread::sleep_for(std::chrono::microseconds(100));
            continue;
        }
        ingest.restart_run();
        for (;;) {
            const auto p = ingest.position();
            if (p.run != old.run) {
                const auto tag = ring.tag(0);
                if (tag == SampleRing::tag_of(p.run, 0)) {
                    const auto observe_until = std::chrono::steady_clock::now() + std::chrono::milliseconds(5);
                    while (ring.tag(0) == tag && std::chrono::steady_clock::now() < observe_until) {
                        const auto current = ingest.position();
                        const auto latest = current.newest ? ring.tag(current.newest - 1) : 0;
                        const auto after = ingest.position();
                        if (current.run == p.run && current.newest && after.run == current.run &&
                            after.newest >= current.newest && after.newest - current.newest < ring.capacity_slices() &&
                            latest != SampleRing::tag_of(current.run, current.newest - 1)) {
                            ++corrupt;
                            break;
                        }
                        std::this_thread::sleep_for(std::chrono::microseconds(50));
                    }
                    ++checked;
                    break;
                }
            }
            if (std::chrono::steady_clock::now() >= deadline) break;
            std::this_thread::sleep_for(std::chrono::microseconds(50));
        }
    }
    stop = true;
    sender.join();
    std::printf("checked=%d corrupt_positions=%d\n", checked, corrupt);
    return checked >= 20 && corrupt == 0 ? 0 : 1;
}
