"""Windows versions of the session's POSIX pieces: process check, session lock, local transport.

Each module imports Windows-only names (msvcrt, ctypes.windll) inside its
functions, so the package imports on every platform and the tests drive it
with fakes on macOS.
"""
