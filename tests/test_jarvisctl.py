"""
Tests for jarvisctl.py's side of the control-socket handshake.

Specifically the one that bit: `jarvis quit` used to report failure on a
completely successful quit. The client reads until EOF on a socket built
with `timeout=3`, and that timeout governs every `recv()`, not just the
connect -- while main.py used to send its reply and then run the whole
goodbye (a blocking spin-down earcon, Piper synthesis and playback, then
llm_intent.unload()) *inside* the connection's `with` block. EOF
therefore arrived late, the second recv() timed out, `socket.timeout`
surfaced as an `OSError`, and the client printed "Can't reach jarvis --
is it running?" and exited 1 -- throwing away the "powering down, sir"
it had already received. Sitting right on the 3s boundary is what made
it intermittent, and intermittent is what made it hard to attribute.

Driven against a mock server on an ephemeral port rather than the real
one, so these never collide with a Jarvis actually running on 47123.
"""

import socket
import threading
import time

import jarvisctl


def _serve_once(handler):
    """
    One-shot control server on an ephemeral port. Returns the port and
    the thread, which the caller joins.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _run():
        try:
            conn, _ = server.accept()
            handler(conn)
        finally:
            server.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return port, thread


def _run_client(monkeypatch, capsys, port, *args):
    """Runs the real jarvisctl.main(), returns (exit_code, stdout)."""
    monkeypatch.setattr(jarvisctl, "PORT", port)
    monkeypatch.setattr(jarvisctl.sys, "argv", ["jarvisctl.py", *args])
    code = 0
    try:
        jarvisctl.main()
    except SystemExit as exc:
        code = exc.code or 0
    return code, capsys.readouterr().out


def test_quit_succeeds_when_the_server_closes_before_announcing(monkeypatch, capsys):
    # main.py's fixed shape: reply, let the connection close, and only
    # then spend several seconds saying goodbye. The client should be
    # done and gone well before that work would have finished.
    announced = threading.Event()

    def handler(conn):
        with conn:
            conn.recv(64)
            conn.sendall(b"powering down, sir")
        # ...connection closed. The slow goodbye happens out here now.
        time.sleep(0.4)
        announced.set()

    port, thread = _serve_once(handler)
    started = time.monotonic()
    code, out = _run_client(monkeypatch, capsys, port, "quit")
    elapsed = time.monotonic() - started

    assert code == 0
    assert "powering down, sir" in out
    assert "Can't reach jarvis" not in out
    # Didn't wait on the announcement, let alone time out behind it.
    assert elapsed < 0.4, f"client waited {elapsed:.2f}s for a reply it already had"
    assert announced.wait(5)
    thread.join(timeout=5)


def test_holding_the_connection_open_past_the_timeout_is_what_used_to_fail(monkeypatch, capsys):
    # The old shape, pinned so the reason the fix matters stays legible:
    # the reply is sent, but EOF is withheld past the client's own 3s
    # deadline, and the already-received reply is discarded.
    #
    # The slowest test in the suite (~4s), unavoidably -- the whole point
    # is to outlast that deadline. Worth it: this is the bug's actual
    # mechanism, and without it the fix reads as an arbitrary reordering.
    def handler(conn):
        with conn:
            conn.recv(64)
            conn.sendall(b"powering down, sir")
            time.sleep(4.0)  # the goodbye, still inside `with conn:`

    port, thread = _serve_once(handler)
    code, out = _run_client(monkeypatch, capsys, port, "quit")

    assert code == 1
    assert "Can't reach jarvis" in out
    assert "powering down, sir" not in out
    thread.join(timeout=10)


def test_a_multi_line_stats_reply_is_read_whole(monkeypatch, capsys):
    # The reason the client loops to EOF rather than doing one recv() --
    # `stats` is comfortably larger than a single guaranteed read.
    body = "\n".join(f"line {i}" for i in range(500))

    def handler(conn):
        with conn:
            conn.recv(64)
            conn.sendall(body.encode())

    port, thread = _serve_once(handler)
    code, out = _run_client(monkeypatch, capsys, port, "stats")

    assert code == 0
    assert "line 0" in out and "line 499" in out
    thread.join(timeout=5)


def test_nothing_listening_reports_honestly(monkeypatch, capsys):
    # Bind and close, so the port is almost certainly free and refusing.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    code, out = _run_client(monkeypatch, capsys, port, "status")

    assert code == 1
    assert "Can't reach jarvis" in out


def test_an_unknown_command_never_reaches_the_socket(monkeypatch, capsys):
    # Validated client-side, so a typo can't be half-interpreted by the
    # server's own `startswith("debug")` handling.
    code, out = _run_client(monkeypatch, capsys, 1, "detonate")
    assert code == 1
    assert "Usage:" in out


def test_quit_silent_reaches_the_server_as_one_command(monkeypatch, capsys):
    # The sleep-triggered task's whole purpose: skip the spoken goodbye
    # so the teardown finishes before Windows suspends the audio stack,
    # rather than straddling the suspend and closing streams onto a
    # driver that is reinitialising on the other side.
    received = []

    def handler(conn):
        with conn:
            received.append(conn.recv(64))
            conn.sendall(b"powering down, sir")

    port, thread = _serve_once(handler)
    code, out = _run_client(monkeypatch, capsys, port, "quit", "silent")

    assert code == 0
    assert received == [b"quit silent"]
    assert "powering down, sir" in out
    thread.join(timeout=5)


def test_a_bare_quit_still_gets_the_goodbye(monkeypatch, capsys):
    # Opt-in, not the default -- anyone quitting by hand wants the
    # spin-down and the sign-off.
    received = []

    def handler(conn):
        with conn:
            received.append(conn.recv(64))
            conn.sendall(b"powering down, sir")

    port, thread = _serve_once(handler)
    code, _out = _run_client(monkeypatch, capsys, port, "quit")

    assert code == 0
    assert received == [b"quit"]
    thread.join(timeout=5)


def test_an_unknown_quit_argument_never_reaches_the_socket(monkeypatch, capsys):
    # Same client-side validation the other arguments get: the server
    # matches "quit silent" exactly, so a typo must not arrive as
    # something it half-recognises.
    code, out = _run_client(monkeypatch, capsys, 1, "quit", "quietly")
    assert code == 1
    assert "Usage:" in out
