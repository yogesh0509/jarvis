"""
Command-line control for the running jarvis main.py instance --
talks to its local control server (localhost only, fixed port) so
there's no tray icon or GUI needed to check status, pause/resume, or
quit.

Usage (from D:\\jarvis):
    .venv\\Scripts\\python.exe jarvisctl.py status
    .venv\\Scripts\\python.exe jarvisctl.py pause
    .venv\\Scripts\\python.exe jarvisctl.py resume
    .venv\\Scripts\\python.exe jarvisctl.py quit
"""

import socket
import sys

HOST = "127.0.0.1"
PORT = 47123
COMMANDS = {"status", "pause", "resume", "quit"}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(f"Usage: jarvisctl.py {{{'|'.join(sorted(COMMANDS))}}}")
        sys.exit(1)

    command = sys.argv[1]
    try:
        with socket.create_connection((HOST, PORT), timeout=3) as sock:
            sock.sendall(command.encode())
            reply = sock.recv(256).decode("utf-8", "ignore")
            print(reply)
    except (ConnectionRefusedError, OSError):
        print("Can't reach jarvis -- is it running?")
        sys.exit(1)


if __name__ == "__main__":
    main()
