/* SPDX-License-Identifier: MIT */
#ifndef W1700K_POSIX_H
#define W1700K_POSIX_H

#include <errno.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <unistd.h>

static inline int w1700k_cloexec(int fd)
{
	if (fd >= 0 && fcntl(fd, F_SETFD, FD_CLOEXEC) < 0) {
		int error = errno;
		close(fd);
		errno = error;
		return -1;
	}
	return fd;
}

static inline int w1700k_socket(int domain, int type, int protocol)
{
#ifdef __APPLE__
	return w1700k_cloexec(socket(domain, type, protocol));
#else
	return socket(domain, type | SOCK_CLOEXEC, protocol);
#endif
}

static inline int w1700k_pipe(int fds[2])
{
#ifdef __APPLE__
	if (pipe(fds)) return -1;
	if (fcntl(fds[0], F_SETFD, FD_CLOEXEC) < 0 || fcntl(fds[1], F_SETFD, FD_CLOEXEC) < 0) {
		int error = errno;
		close(fds[0]);
		close(fds[1]);
		errno = error;
		return -1;
	}
	return 0;
#else
	return pipe2(fds, O_CLOEXEC);
#endif
}

#endif
