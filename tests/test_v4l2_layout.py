#!/usr/bin/env python3
"""
linux-privacy-switch — offline regression tests for the V4L2 ioctl layout.

No camera, no root, no hardware: these only check that every buffer handed to
fcntl.ioctl() is exactly as large as its request code says, and that the field
offsets match linux/videodev2.h (64-bit). A buffer that is too small is what
made the daemon crash on Python 3.14 with SystemError("buffer overflow").

Usage: python3 tests/test_v4l2_layout.py
"""

import importlib.util
import pathlib
import re
import struct
import unittest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "privacy-switch.py"
_spec = importlib.util.spec_from_file_location("privacy_switch", SRC)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


# Struct sizes as encoded in the ioctl request codes of linux/videodev2.h.
EXPECTED_SIZES = {
    "VIDIOC_QUERYCAP":  104,   # struct v4l2_capability
    "VIDIOC_S_FMT":     208,   # struct v4l2_format
    "VIDIOC_REQBUFS":    20,   # struct v4l2_requestbuffers
    "VIDIOC_QUERYBUF":   88,   # struct v4l2_buffer
    "VIDIOC_QBUF":       88,
    "VIDIOC_DQBUF":      88,
    "VIDIOC_STREAMON":    4,   # int (buffer type)
    "VIDIOC_STREAMOFF":   4,
}

# Field offsets from linux/videodev2.h, 64-bit layout.
EXPECTED_OFFSETS = {
    "CAP_CAPABILITIES": (84, "VIDIOC_QUERYCAP"),
    "CAP_DEVICE_CAPS":  (88, "VIDIOC_QUERYCAP"),
    "FMT_TYPE":         (0,  "VIDIOC_S_FMT"),
    "FMT_WIDTH":        (8,  "VIDIOC_S_FMT"),
    "FMT_HEIGHT":       (12, "VIDIOC_S_FMT"),
    "FMT_PIXELFORMAT":  (16, "VIDIOC_S_FMT"),
    "REQ_COUNT":        (0,  "VIDIOC_REQBUFS"),
    "REQ_TYPE":         (4,  "VIDIOC_REQBUFS"),
    "REQ_MEMORY":       (8,  "VIDIOC_REQBUFS"),
    "BUF_INDEX":        (0,  "VIDIOC_QUERYBUF"),
    "BUF_TYPE":         (4,  "VIDIOC_QUERYBUF"),
    "BUF_MEMORY":       (60, "VIDIOC_QUERYBUF"),
    "BUF_M_OFFSET":     (64, "VIDIOC_QUERYBUF"),
    "BUF_LENGTH":       (72, "VIDIOC_QUERYBUF"),
}


class TestIoctlSizes(unittest.TestCase):
    def test_ioc_size_decodes_request_codes(self):
        for name, size in EXPECTED_SIZES.items():
            with self.subTest(ioctl=name):
                self.assertEqual(ps._ioc_size(getattr(ps, name)), size)

    def test_buffers_match_the_size_the_kernel_expects(self):
        """A short buffer is what Python 3.14 reports as 'buffer overflow'."""
        for name, size in EXPECTED_SIZES.items():
            with self.subTest(ioctl=name):
                self.assertEqual(len(ps._ioc_buffer(getattr(ps, name))), size)

    def test_no_hand_sized_ioctl_buffers_in_the_source(self):
        """
        Every ioctl buffer must come from _ioc_buffer(); a literal size drifts
        away from the struct sooner or later, which is how REQBUFS ended up
        4 bytes short.
        """
        hand_sized = re.findall(r"bytearray\(\s*\d+\s*\)", SRC.read_text())
        self.assertEqual(hand_sized, [])

    def test_buf_type_arg(self):
        for name in ("VIDIOC_STREAMON", "VIDIOC_STREAMOFF"):
            with self.subTest(ioctl=name):
                arg = ps._buf_type_arg(getattr(ps, name))
                self.assertIsInstance(arg, bytearray)
                self.assertEqual(len(arg), 4)
                self.assertEqual(struct.unpack_from("<I", arg, 0)[0], ps.V4L2_BUF_TYPE)


class TestFieldOffsets(unittest.TestCase):
    def test_offsets_match_videodev2_h(self):
        for name, (offset, _) in EXPECTED_OFFSETS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(ps, name), offset)

    def test_every_field_fits_inside_its_struct(self):
        for name, (_, ioctl_name) in EXPECTED_OFFSETS.items():
            with self.subTest(field=name):
                size = ps._ioc_size(getattr(ps, ioctl_name))
                self.assertLessEqual(getattr(ps, name) + 4, size)


if __name__ == "__main__":
    unittest.main(verbosity=2)
