"""Read Codex's plain and Zstandard rollouts without changing source files."""
from __future__ import annotations
import ctypes
import hashlib
import io
import os
from pathlib import Path
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ZSTD_ARCHIVE = 'zstd-v1.5.7-win64.zip'
ZSTD_SHA256 = 'acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9'
_CODEC = None


def is_transcript(path):
    return Path(path).name.lower().endswith(('.jsonl', '.jsonl.zst'))


def plain_path(path):
    path = Path(path)
    return path.with_suffix('') if path.name.lower().endswith('.jsonl.zst') else path


def physical_path(path):
    # Match Codex: a plain sibling takes precedence during compression changes.
    plain = plain_path(path)
    compressed = Path(str(plain) + '.zst')
    return plain if plain.is_file() else compressed if compressed.is_file() else Path(path)


def preferred(path):
    return not Path(path).name.lower().endswith('.jsonl.zst') or not plain_path(path).is_file()


class Buffer(ctypes.Structure):
    _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]


def codec():
    global _CODEC
    if _CODEC is not None:
        return _CODEC
    archive_path = ROOT / 'vendor' / ZSTD_ARCHIVE
    if not archive_path.is_file() or hashlib.sha256(archive_path.read_bytes()).hexdigest() != ZSTD_SHA256:
        raise RuntimeError('压缩会话解码组件缺失或校验失败。请完整解压当前工具包，保留 vendor 文件夹。')
    with zipfile.ZipFile(archive_path) as archive:
        data = archive.read('zstd-v1.5.7-win64/dll/libzstd.dll')
    runtime = ROOT / '.runtime'
    runtime.mkdir(exist_ok=True)
    path = runtime / 'libzstd-1.5.7.dll'
    if not path.is_file() or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest():
        temporary = runtime / ('zstd-' + uuid.uuid4().hex + '.writing')
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        finally:
            if temporary.exists(): temporary.unlink()
    lib = ctypes.CDLL(str(path.resolve()))
    lib.ZSTD_createDStream.argtypes = []
    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(Buffer), ctypes.POINTER(Buffer)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_freeDStream.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    lib.ZSTD_getErrorName.restype = ctypes.c_char_p
    _CODEC = lib
    return lib


def checked(lib, result):
    if lib.ZSTD_isError(result):
        raise RuntimeError('压缩会话文件损坏或格式不支持：' + lib.ZSTD_getErrorName(result).decode('utf-8', 'replace'))
    return result


class ZstdReader(io.RawIOBase):
    """Bounded-memory decoder for standard, concatenated and seekable frames."""
    def __init__(self, path):
        super().__init__()
        self.lib = codec()
        self.ctx = self.lib.ZSTD_createDStream()
        self.source = None
        if not self.ctx:
            raise MemoryError('无法创建压缩会话解码器。')
        try:
            checked(self.lib, self.lib.ZSTD_initDStream(self.ctx))
            self.source = Path(path).open('rb')
        except BaseException:
            self.close()
            raise
        self.input_data = None
        self.input = Buffer(None, 0, 0)
        self.eof = False
        self.frame_complete = False

    def readable(self):
        return True

    def readinto(self, destination):
        if self.closed:
            raise ValueError('Read of closed compressed session')
        if not len(destination):
            return 0
        output_data = (ctypes.c_char * len(destination)).from_buffer(destination)
        output = Buffer(ctypes.addressof(output_data), len(destination), 0)
        while output.pos == 0:
            if self.input.pos == self.input.size and not self.eof:
                block = self.source.read(128 * 1024)
                self.eof = not block
                self.input_data = ctypes.create_string_buffer(block) if block else None
                self.input = Buffer(ctypes.addressof(self.input_data) if block else None, len(block), 0)
            if self.eof and self.input.pos == self.input.size and self.frame_complete:
                return 0
            remaining = checked(self.lib, self.lib.ZSTD_decompressStream(self.ctx, ctypes.byref(output), ctypes.byref(self.input)))
            self.frame_complete = remaining == 0
            if self.eof and output.pos == 0:
                if not self.frame_complete:
                    raise RuntimeError('压缩会话文件不完整或已截断；请保留原文件，重新导出。')
                return 0
        return output.pos

    def close(self):
        if not self.closed:
            if self.source is not None:
                self.source.close()
            if self.ctx:
                self.lib.ZSTD_freeDStream(self.ctx)
                self.ctx = None
        super().close()


def open_transcript(path):
    path = physical_path(path)
    if path.name.lower().endswith('.jsonl.zst'):
        return io.BufferedReader(ZstdReader(path), buffer_size=128 * 1024)
    return path.open('rb')
