"""What this machine can tell about its own network, without contacting anyone."""
from __future__ import annotations

import re
import socket

# Documentation addresses (RFC 5737, RFC 3849): looking up a route to them
# shows whether any network is attached. A datagram socket's connect only
# asks the system for a route; no packet leaves.
_PROBES = ((socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9)))

# The system's own names for "the network is not there", as tools print them.
_NETWORK_ERROR = re.compile(
    r"\b(?:ENOTFOUND|EAI_AGAIN|EAI_NONAME|ENETUNREACH|ENETDOWN|EHOSTUNREACH|EHOSTDOWN|ECONNREFUSED|ECONNRESET|"
    r"ECONNABORTED|ETIMEDOUT|ENOTCONN)\b|getaddrinfo|network is unreachable|no route to host", re.IGNORECASE)


def route_available() -> bool:
    """Whether this machine has a route to a public address."""
    for family, address in _PROBES:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.connect(address)
                return True
        except OSError:
            continue
    return False


def names_network_error(text) -> bool:
    """Whether a tool's last words carry one of the system's network errors."""
    return isinstance(text, str) and _NETWORK_ERROR.search(text[-2000:]) is not None
