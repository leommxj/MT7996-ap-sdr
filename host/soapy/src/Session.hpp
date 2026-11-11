// SPDX-License-Identifier: MIT
#pragma once

#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <sys/types.h>

namespace w1700k {

class CommandSession {
public:
    explicit CommandSession(const std::string &command);
    ~CommandSession();
    CommandSession(const CommandSession &) = delete;
    CommandSession &operator=(const CommandSession &) = delete;

    nlohmann::json request(nlohmann::json message, std::chrono::milliseconds timeout);

private:
    void stop();
    std::runtime_error failure(const std::string &why) const;

    pid_t pid_ = -1;
    int socket_ = -1, stderr_ = -1;
    uint64_t next_id_ = 0;
    std::string output_, errors_;
};

}  // namespace w1700k
