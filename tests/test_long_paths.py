"""Tests for copying a solved project back when its paths are long.

A driven project's results sit several folders deep, and under a long run
folder the copy back overran Windows' 260-character limit (WinError 3).

Run: python -m unittest discover -s tests
"""
import os
import tempfile
import unittest

from microwave_package.workflow import postprocess


class TestLongPaths(unittest.TestCase):
    def test_the_prefix_is_added_once_and_only_on_windows(self):
        plain = os.path.abspath("somewhere")
        prefixed = postprocess.long_path(plain)
        self.assertEqual(postprocess.long_path(prefixed), prefixed)
        if os.name == "nt":
            self.assertTrue(prefixed.startswith(postprocess.LONG_PREFIX))
        else:
            self.assertEqual(prefixed, plain)

    @unittest.skipUnless(os.name == "nt", "the 260-character limit is Windows'")
    def test_a_tree_deeper_than_the_limit_is_copied_and_verified(self):
        source = tempfile.mkdtemp(prefix="src_")
        deep = os.path.join(postprocess.long_path(source), *["d" * 40] * 6)
        os.makedirs(deep)
        with open(os.path.join(deep, "result.dat"), "w") as handle:
            handle.write("x" * 100)
        destination = os.path.join(tempfile.mkdtemp(prefix="dst_"), "incoming")
        self.assertGreater(len(os.path.join(destination, *["d" * 40] * 6, "result.dat")), 260)
        self.assertEqual(postprocess.copy_tree_verified(source, destination), [])
        copied = os.path.join(postprocess.long_path(destination), *["d" * 40] * 6, "result.dat")
        self.assertEqual(os.path.getsize(copied), 100)


if __name__ == "__main__":
    unittest.main()
