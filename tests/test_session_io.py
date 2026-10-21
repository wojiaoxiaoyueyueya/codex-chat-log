"""Synthetic Zstandard fixtures only; no personal conversations."""
import ctypes
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib'))
import session_io as session


def compressed(data):
    lib = session.codec()
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(data)))
    source = ctypes.create_string_buffer(data)
    size = session.checked(lib, lib.ZSTD_compress(output, len(output), source, len(data), 3))
    return output.raw[:size]


class SessionIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_chunked_unicode_large_lines_and_readonly_source(self):
        data = ('中文 ' * 120000 + '\nnext line\n').encode()
        path = self.root / 'rollout-test.jsonl.zst'
        original = compressed(data)
        path.write_bytes(original)
        with session.open_transcript(path.with_suffix('')) as stream:
            self.assertEqual(stream.read(7) + stream.readline() + stream.read(), data)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.with_suffix('').exists())

    def test_seekable_concatenated_frames_and_skippable_index(self):
        chunks = [b'first\n', b'second\n', b'third\n']
        frames = [compressed(data) for data in chunks]
        index = b''.join(struct.pack('<II', len(frame), len(data)) for frame, data in zip(frames, chunks))
        index += struct.pack('<IBI', len(frames), 0, 0x8F92EAB1)
        path = self.root / 'seekable.jsonl.zst'
        path.write_bytes(b''.join(frames) + struct.pack('<II', 0x184D2A5E, len(index)) + index)
        with session.open_transcript(path) as stream:
            self.assertEqual(list(stream), chunks)

    def test_plain_sibling_precedes_stale_compressed_sibling(self):
        plain = self.root / 'same.jsonl'
        plain.write_bytes(b'current\n')
        archive = Path(str(plain) + '.zst')
        archive.write_bytes(b'not a valid zstd frame')
        with session.open_transcript(archive) as stream:
            self.assertEqual(stream.read(), b'current\n')
        self.assertFalse(session.preferred(archive))

    def test_corrupt_empty_truncated_and_trailing_garbage_fail(self):
        data = compressed(b'line\n' * 1000)
        for index, raw in enumerate((b'broken', b'', data[:-1], data + b'garbage')):
            with self.subTest(index=index):
                path = self.root / ('bad%d.jsonl.zst' % index)
                path.write_bytes(raw)
                with self.assertRaisesRegex(RuntimeError, '压缩会话'):
                    with session.open_transcript(path) as stream:
                        stream.read()

    def test_early_close_releases_file_and_decoder(self):
        path = self.root / 'close.jsonl.zst'
        path.write_bytes(compressed(b'one\n' * 100000))
        stream = session.open_transcript(path)
        raw = stream.raw
        self.assertEqual(stream.readline(), b'one\n')
        stream.close()
        self.assertTrue(raw.source.closed)
        self.assertIsNone(raw.ctx)
        path.unlink()

    def test_missing_or_unverified_decoder_fails_without_loading(self):
        with patch.object(session, '_CODEC', None), patch.object(session, 'ROOT', self.root):
            with self.assertRaisesRegex(RuntimeError, '解码组件缺失或校验失败'):
                session.codec()


if __name__ == '__main__':
    unittest.main()
