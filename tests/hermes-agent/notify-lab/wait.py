#!/usr/bin/env python3
"""Deadline-bound polling; commands must return promptly and never print secrets."""
import argparse
import subprocess
import sys
import time


def wait(command, seconds, interval):
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=remaining, check=False)
        except subprocess.TimeoutExpired:
            return False
        matched = result.returncode == 0
        if matched:
            return True
        remaining = deadline - time.monotonic()
        if 0 < remaining <= interval:
            time.sleep(remaining)
            return False
        if remaining > 0:
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command or args.timeout <= 0 or args.interval <= 0:
        parser.error('command, positive timeout and positive interval required')
    if not wait(command, args.timeout, args.interval):
        print('wait failed: deadline exceeded', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
