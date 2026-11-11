"""The floating subtitle overlay: a native window above other apps that shows the current line.

``control`` opens and closes the overlay process from the plugin side;
``__main__`` is that process. ``model``, ``placement`` and ``app`` hold the
logic without AppKit; ``panel_mac`` is the macOS window.
"""
