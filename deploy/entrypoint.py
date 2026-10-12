#!/usr/bin/env python3
"""Start the container's command as the bot's own user, not as root.

A volume arrives owned by root, so the folder the bot keeps its files in, which is the one
this is started in, is handed to that user first. Then root is given up for good and the
command takes this process's place: nothing is left running as root.

In Python, and with the standard library alone, so that it leans on nothing the base image
may or may not ship.
"""

import os
import pwd
import sys

USER = os.environ.get("ZBAY_USER", "zbay")


def main(command):
    user = pwd.getpwnam(USER)

    if os.getuid() == 0:
        os.chown(os.getcwd(), user.pw_uid, user.pw_gid)
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    os.environ["HOME"] = os.getcwd()
    os.execvp(command[0], command)


if __name__ == "__main__":
    main(sys.argv[1:])
