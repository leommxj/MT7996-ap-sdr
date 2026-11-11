// SPDX-License-Identifier: MIT
#include "Session.hpp"
#include "w1700k_posix.h"

#include <cerrno>
#include <csignal>
#include <cstring>
#include <poll.h>
#include <spawn.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <thread>

extern char **environ;

namespace w1700k {

CommandSession::CommandSession(const std::string &command)
{
    int sockets[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets))
        throw std::runtime_error(std::string("control socket: ") + std::strerror(errno));
    int errors[2];
    if (w1700k_pipe(errors)) {
        close(sockets[0]);
        close(sockets[1]);
        throw std::runtime_error(std::string("control stderr pipe: ") + std::strerror(errno));
    }
    fcntl(sockets[0], F_SETFD, FD_CLOEXEC);
    fcntl(sockets[1], F_SETFD, FD_CLOEXEC);
#ifdef __APPLE__
    const int enabled = 1;
    setsockopt(sockets[0], SOL_SOCKET, SO_NOSIGPIPE, &enabled, sizeof(enabled));
#endif
    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    short flags = POSIX_SPAWN_SETPGROUP;
#ifdef __APPLE__
    flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
    posix_spawnattr_setflags(&attributes, flags);
    posix_spawnattr_setpgroup(&attributes, 0);
    posix_spawn_file_actions_t actions;
    posix_spawn_file_actions_init(&actions);
    posix_spawn_file_actions_adddup2(&actions, sockets[1], STDIN_FILENO);
    posix_spawn_file_actions_adddup2(&actions, sockets[1], STDOUT_FILENO);
    posix_spawn_file_actions_adddup2(&actions, errors[1], STDERR_FILENO);
    char *argv[] = {const_cast<char *>(command.c_str()), const_cast<char *>("serve"), nullptr};
    const int spawned = posix_spawnp(&pid_, argv[0], &actions, &attributes, argv, environ);
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attributes);
    close(sockets[1]);
    close(errors[1]);
    if (spawned) {
        close(sockets[0]);
        close(errors[0]);
        throw std::runtime_error("cannot run " + command + ": " + std::strerror(spawned));
    }
    socket_ = sockets[0];
    stderr_ = errors[0];
    fcntl(socket_, F_SETFL, fcntl(socket_, F_GETFL) | O_NONBLOCK);
    fcntl(stderr_, F_SETFL, fcntl(stderr_, F_GETFL) | O_NONBLOCK);
}

CommandSession::~CommandSession() { stop(); }

void CommandSession::stop()
{
    if (socket_ >= 0) shutdown(socket_, SHUT_WR);
    if (pid_ > 0) {
        auto wait_for = [&](std::chrono::milliseconds grace) {
            const auto deadline = std::chrono::steady_clock::now() + grace;
            int status;
            do {
                const pid_t done = waitpid(pid_, &status, WNOHANG);
                if (done == pid_ || (done < 0 && errno == ECHILD)) return true;
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            } while (std::chrono::steady_clock::now() < deadline);
            return false;
        };
        if (!wait_for(std::chrono::milliseconds(500))) {
            kill(-pid_, SIGTERM);
            if (!wait_for(std::chrono::milliseconds(100))) {
                kill(-pid_, SIGKILL);
                while (waitpid(pid_, nullptr, 0) < 0 && errno == EINTR) {}
            }
        }
        pid_ = -1;
    }
    if (socket_ >= 0) close(socket_);
    if (stderr_ >= 0) close(stderr_);
    socket_ = stderr_ = -1;
}

std::runtime_error CommandSession::failure(const std::string &why) const
{
    const auto end = errors_.find_last_not_of(" \r\n");
    if (end == std::string::npos) return std::runtime_error("w1700k serve: " + why);
    const auto newline = errors_.find_last_of('\n', end);
    const auto from = newline == std::string::npos ? 0 : newline + 1;
    return std::runtime_error("w1700k serve: " + why + ": " +
                              errors_.substr(from, end - from + 1));
}

nlohmann::json CommandSession::request(nlohmann::json message, std::chrono::milliseconds timeout)
{
    const uint64_t id = ++next_id_;
    message["id"] = id;
    const std::string input = message.dump() + '\n';
    size_t sent = 0;
    errors_.clear();
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    for (;;) {
        size_t newline;
        while ((newline = output_.find('\n')) != std::string::npos) {
            const std::string line = output_.substr(0, newline);
            output_.erase(0, newline + 1);
            if (line.find_first_not_of(" \r\t") == std::string::npos) continue;
            auto reply = nlohmann::json::parse(line, nullptr, false);
            if (!reply.is_object()) throw failure("invalid JSON reply");
            if (!reply.contains("id") && reply.contains("event")) continue;
            if (!reply.contains("id") || reply["id"] != id || !reply.contains("ok") || !reply["ok"].is_boolean())
                throw failure("unexpected reply id or missing result");
            return reply;
        }
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
        if (left.count() <= 0) throw failure("timed out after " + std::to_string(timeout.count()) + " ms");
        pollfd fds[] = {{socket_, short(POLLIN | (sent < input.size() ? POLLOUT : 0)), 0}, {stderr_, POLLIN, 0}};
        const int ready = poll(fds, 2, int(left.count()));
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) throw failure(std::strerror(errno));
        char buffer[8192];
        if (fds[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            ssize_t n;
            while ((n = read(stderr_, buffer, sizeof(buffer))) > 0) errors_.append(buffer, size_t(n));
            if (errors_.size() > 65536) errors_.erase(0, errors_.size() - 65536);
            if (n == 0) { close(stderr_); stderr_ = -1; }
        }
        if (fds[0].revents & POLLOUT) {
#ifdef MSG_NOSIGNAL
            constexpr int flags = MSG_NOSIGNAL;
#else
            constexpr int flags = 0;
#endif
            const ssize_t n = send(socket_, input.data() + sent, input.size() - sent, flags);
            if (n > 0) sent += size_t(n);
            else if (n < 0 && errno != EINTR && errno != EAGAIN) throw failure("control process closed its input");
        }
        if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            const ssize_t n = recv(socket_, buffer, sizeof(buffer), 0);
            if (n > 0) {
                output_.append(buffer, size_t(n));
                if (output_.size() > 1024 * 1024) throw failure("reply exceeds 1 MiB");
            } else if (n == 0 || (errno != EINTR && errno != EAGAIN)) {
                throw failure("control process ended");
            }
        }
    }
}

}  // namespace w1700k
