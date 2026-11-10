/* Preserve the previous launcher's bounded account-file reads before libc
 * lookup. These paths are immutable in the read-only PostgreSQL container. */
#include <fcntl.h>
#include <sys/stat.h>

#define ACCOUNT_FILE_LIMIT (10u << 20)
#define ACCOUNT_LINE_LIMIT (64u << 10)

static void check_account_file(const char *path)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NONBLOCK | O_NOFOLLOW);
	struct stat info;
	char buffer[4096];
	size_t total = 0, line = 0;
	ssize_t length;

	if (fd < 0)
		err(1, "open account file %s", path);
	if (fstat(fd, &info) < 0)
		err(1, "stat account file %s", path);
	if (!S_ISREG(info.st_mode) || info.st_size < 0 ||
	    (unsigned long long)info.st_size > ACCOUNT_FILE_LIMIT)
		errx(1, "account file exceeds limit or is not regular: %s", path);
	while (1) {
		length = read(fd, buffer, sizeof(buffer));
		if (length < 0 && errno == EINTR)
			continue;
		if (length < 0)
			err(1, "read account file %s", path);
		if (length == 0)
			break;
		total += (size_t)length;
		if (total > ACCOUNT_FILE_LIMIT)
			errx(1, "account file exceeds limit: %s", path);
		for (ssize_t i = 0; i < length; i++) {
			if (buffer[i] == '\n')
				line = 0;
			else if (++line >= ACCOUNT_LINE_LIMIT)
				errx(1, "account record exceeds limit: %s", path);
		}
	}
	if (close(fd) < 0)
		err(1, "close account file %s", path);
}
