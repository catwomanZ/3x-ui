import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import subserver  # noqa: E402


class SubserverTest(unittest.TestCase):
    def test_stable_paths_map_to_each_private_format(self):
        with patch.multiple(
            subserver,
            PUBLIC_PATH="/sub/",
            PUBLIC_JSON_PATH="/sub-json/",
            PUBLIC_CLASH_PATH="/sub-clash/",
            SUB_REAL_PATH="/raw-random/",
            SUB_JSON_REAL_PATH="/json-random/",
            SUB_CLASH_REAL_PATH="/clash-random/",
            DEFAULT_SUBID="CATWOMAN",
        ):
            cases = {
                "/sub": "/raw-random/CATWOMAN",
                "/sub/": "/raw-random/CATWOMAN",
                "/sub/alice": "/raw-random/alice",
                "/sub-json": "/json-random/CATWOMAN",
                "/sub-json/alice": "/json-random/alice",
                "/sub-clash/": "/clash-random/CATWOMAN",
                "/sub-clash/alice": "/clash-random/alice",
            }
            for public, private in cases.items():
                self.assertEqual(subserver.to_upstream(public), private, public)

    def test_zero_quota_header_is_not_forwarded(self):
        empty = subserver.parse_userinfo(
            "upload=0; download=0; total=0; expire=0"
        )
        quota = subserver.parse_userinfo(
            "upload=3; download=4; total=100; expire=0"
        )
        expiry = subserver.parse_userinfo(
            "upload=0; download=0; total=0; expire=2000000000"
        )
        self.assertFalse(subserver.has_quota(empty))
        self.assertTrue(subserver.has_quota(quota))
        self.assertTrue(subserver.has_quota(expiry))
        self.assertIsNone(subserver.parse_userinfo("not-a-header"))


if __name__ == "__main__":
    unittest.main()
