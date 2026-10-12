"""What an HTTP header value can hold, for the paths that check one before it is sent."""

from __future__ import annotations

import re

# Printable ASCII, with spaces and tabs only between other characters (RFC 9110's field value,
# without obsolete text); the empty value matches. The HTTP clients refuse most other values (a
# space at either end, a line break, NUL, non-ASCII) with an error that can quote the value, so
# a value is checked against this before it is stored or sent.
HEADER_VALUE_RE = re.compile(r"(?:[!-~](?:[ \x09!-~]*[!-~])?)?")
