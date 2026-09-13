"""
Command-line control for the running jarvis main.py instance --
talks to its local control server (localhost only, fixed port) so
there's no tray icon or GUI needed to check status, pause/resume,
quit, or basic usage stats.

Usage (from D:\\jarvis):
    .venv\\Scripts\\python.exe jarvisctl.py status
    .venv\\Scripts\\python.exe jarvisctl.py stats
    .venv\\Scripts\\python.exe jarvisctl.py pause
    .venv\\Scripts\\python.exe jarvisctl.py resume
    .venv\\Scripts\\python.exe jarvisctl.py quit
"""

import socket
import sys

HOST = "127.0.0.1"
PORT = 47123
COMMANDS = {"status", "stats", "debug", "pause", "resume", "quit"}
# "debug" alone toggles; "debug on"/"debug off" set it explicitly. It's
# the one command that takes an argument, hence the argv handling below
# being slightly less blunt than a length check.
ARGUMENTS = {"debug": {"on", "off"}}


def main():
    args = sys.argv[1:]
    valid = (
        1 <= len(args) <= 2
        and args[0] in COMMANDS
        and (len(args) == 1 or args[1] in ARGUMENTS.get(args[0], set()))
    )
    if not valid:
        print(f"Usage: jarvisctl.py {{{'|'.join(sorted(COMMANDS))}}}")
        print("       jarvisctl.py debug [on|off]")
        sys.exit(1)

    command = " ".join(args)
    try:
        with socket.create_connection((HOST, PORT), timeout=3) as sock:
            sock.sendall(command.encode())
            # Looped, not a single fixed-size recv() -- "stats" can be a
            # multi-line reply well past what one read is guaranteed to
            # return; the server closes the connection right after
            # sending (see main.py's control loop), so reading until
            # recv() returns empty reliably gets all of it regardless of
            # size.
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            reply = b"".join(chunks).decode("utf-8", "ignore")
            print(reply)
    except (ConnectionRefusedError, OSError):
        print("Can't reach jarvis -- is it running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
