"""The application's colours.

This is a macOS-only tool that talks to an iPhone, so it uses Apple's own system
palette rather than a set of colours chosen by eye. Every value below is a documented
macOS dynamic system colour in its dark appearance -- ``systemBlue``, ``systemRed``,
``systemGreen``, ``systemOrange``, and the three background levels -- which means the
app looks like it belongs next to the Photos app it is standing in for, and the red
that guards deletion is the same red macOS uses for the same purpose everywhere else.

Textual ships twenty-one themes of its own and the command palette (``ctrl+p``) can
switch to any of them, so nobody is stuck with this one. What is *not* wanted is a
theme setting of our own: a picker is a lifetime of maintenance for no function, and
Textual already has one.
"""

from __future__ import annotations

from textual.theme import Theme

__all__ = ["APPLE_DARK", "THEME_NAME"]

THEME_NAME = "apple-dark"

APPLE_DARK = Theme(
    name=THEME_NAME,
    # systemBlue / systemIndigo: the accent macOS puts on its own primary buttons.
    primary="#0A84FF",
    secondary="#5E5CE6",
    accent="#0A84FF",
    # The three semantic colours, used only where they mean something. They are
    # never the *only* signal -- see the marker column in the activity log, which
    # is what carries the meaning when a terminal has no colour at all.
    warning="#FF9F0A",  # systemOrange
    error="#FF453A",  # systemRed
    success="#30D158",  # systemGreen
    # systemBackground, secondarySystemBackground, tertiarySystemBackground.
    background="#1C1C1E",
    surface="#2C2C2E",
    panel="#3A3A3C",
    foreground="#F2F2F7",
    dark=True,
)
