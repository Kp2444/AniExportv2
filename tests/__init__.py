"""Test package.

Blocks outbound network access for the whole suite. Two tests were silently
reaching the live AniList API because their mocks patched a name the code under
test never calls - one patched `resolve_manga_entries` while the endpoint used
`resolve_many_entries`, the other left the sequel-expansion path unstubbed. Both
passed, both burned rate limit on every run, and both would have failed offline.

A vacuous mock now raises here instead of quietly phoning home. Loopback stays
open: several tests start a real HTTP server and talk to it.
"""
import socket

_real_connect = socket.socket.connect
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host not in _LOCAL:
        raise AssertionError(
            "the test suite tried to reach %s. Nothing here may touch the "
            "network: if a test needs AniList, stub the function the code "
            "actually calls. Live checks belong in the scratchpad harness." % (host,))
    return _real_connect(self, address)


socket.socket.connect = _guarded_connect
