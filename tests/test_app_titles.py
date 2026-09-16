"""
Tests for app_titles' per-app title parsers -- table-driven over titles
captured live from the real machine (see actions.describe_screen /
window_context.py's docstrings for the same "validated against live
titles" convention), plus garbage inputs no parser may raise on.
"""

import pytest

import app_titles


@pytest.mark.parametrize(
    "title, expected",
    [
        (
            "main.py (Working Tree) (main.py) - jarvis - Visual Studio Code",
            "main.py, in jarvis",
        ),
        (
            "address-to-scripthash.md - notes [WSL: Ubuntu-24.04] - Visual Studio Code",
            "address-to-scripthash.md, in notes",
        ),
        (
            "Screen awareness and pro… - jarvis - Visual Studio Code",
            "Screen awareness and pro, in jarvis",
        ),
        ("Visual Studio Code", "Visual Studio Code"),  # a bare window with nothing more specific to strip
        ("", None),
    ],
)
def test_describe_vscode(title, expected):
    assert app_titles.describe_vscode(title) == expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("shannon entropy - Google Search - Google Chrome", "shannon entropy"),
        ("New Tab - Google Chrome", "New Tab"),
        ("Lexer and Parser Explained - Google Chrome", "Lexer and Parser Explained"),
        # A PWA window carries no " - Google Chrome" suffix at all.
        ("Process spending authority and consensus bounds - Claude", "Process spending authority and consensus bounds - Claude"),
        ("(3) Inbox - Google Chrome", "Inbox"),
        ("", None),
    ],
)
def test_describe_chrome(title, expected):
    assert app_titles.describe_chrome(title) == expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Spotify Premium", None),
        ("Spotify", None),
        ("Spotify Free", None),
        ("Nanku - Pyar Tum", '"Pyar Tum" by Nanku'),
        ("", None),
    ],
)
def test_describe_spotify(title, expected):
    assert app_titles.describe_spotify(title) == expected


def test_describe_discord():
    assert app_titles.describe_discord("@sayam123456789 - Discord") == "@sayam123456789"
    assert app_titles.describe_discord("#general - Discord") == "#general"


def test_describe_generic():
    assert app_titles.describe_generic("  Program Manager  ") == "Program Manager"
    assert app_titles.describe_generic("") is None
    assert app_titles.describe_generic(None) is None


def test_describe_title_dispatches_by_app_key():
    assert app_titles.describe_title("vscode", "main.py - proj - Visual Studio Code") == "main.py, in proj"
    assert app_titles.describe_title("chrome", "Test - Google Chrome") == "Test"
    assert app_titles.describe_title(None, "Some Window") == "Some Window"
    assert app_titles.describe_title("unregistered_app", "Whatever") == "Whatever"


@pytest.mark.parametrize("garbage", ["", " - ", "- Google Chrome", "-" * 20, "…" * 10, "(" * 5, "a" * 500])
def test_no_parser_raises_on_garbage(garbage):
    for app_key in (None, "vscode", "chrome", "spotify", "discord"):
        app_titles.describe_title(app_key, garbage)  # must not raise
