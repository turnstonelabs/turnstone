"""Tests for turnstone.core.hash_ring."""

from turnstone.core.hash_ring import bucket_of


class TestBucketOf:
    def test_known_vectors(self):
        assert bucket_of("a3f1" + "0" * 28) == 0xA3F1
        assert bucket_of("0000" + "a" * 28) == 0
        assert bucket_of("ffff" + "b" * 28) == 65535

    def test_hex_prefix(self):
        # Only the first 4 hex chars matter — the rest is ignored.
        assert bucket_of("abcd0000") == bucket_of("abcdffff")
        assert bucket_of("abcd0000") == 0xABCD
