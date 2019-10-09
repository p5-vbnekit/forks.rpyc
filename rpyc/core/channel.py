"""*Channel* is an abstraction layer over streams that works with *packets of data*,
rather than an endless stream of bytes, and adds support for compression.
"""
from rpyc.lib import safe_import
from rpyc.lib.compat import Struct, BYTES_LITERAL
from rpyc.core import brine
import io, ctypes, threading
zlib = safe_import("zlib")

# * 64 bit length field?
# * separate \n into a FlushingChannel subclass?
# * add thread safety as a subclass?


class Channel(object):
    """Channel implementation.

    Note: In order to avoid problems with all sorts of line-buffered transports,
    we deliberately add ``\\n`` at the end of each frame.
    """

    COMPRESSION_THRESHOLD = 3000
    COMPRESSION_LEVEL = 1
    FRAME_HEADER = Struct("!LB")
    FLUSHER = BYTES_LITERAL("\n")  # cause any line-buffered layers below us to flush
    __slots__ = ["stream", "__compression", "__rapid_io_context"]

    def __init__(self, stream, compress=True):
        self.stream = stream
        self.__compression = compress and bool(zlib)
        self.__rapid_io_context = self.__RapidIoContext(self)

    @property
    def compression(self): return self.__compression
    @compression.setter
    def compression(self, value):
      if value is None: self.__compression = bool(zlib)
      else:
        if not (isinstance(value, bool)): raise TypeError("invalid value, boolean expected")
        self.__compression = bool(zlib) and value

    @property
    def max_io_chunk(self): return self.stream.max_io_chunk
    @max_io_chunk.setter
    def max_io_chunk(self, value): self.stream.max_io_chunk = value

    @property
    def raw_handler(self): return self.__rapid_io_context.raw_handler
    @raw_handler.setter
    def raw_handler(self, value): self.__rapid_io_context.raw_handler = value

    def raw_write(self, *args, **kwargs): return self.__rapid_io_context.raw_write(*args, **kwargs)

    def close(self):
        """closes the channel and underlying stream"""
        self.stream.close()

    @property
    def closed(self):
        """indicates whether the underlying stream has been closed"""
        return self.stream.closed

    def fileno(self):
        """returns the file descriptor of the underlying stream"""
        return self.stream.fileno()

    def poll(self, timeout):
        """polls the underlying steam for data, waiting up to *timeout* seconds"""
        return self.stream.poll(timeout)

    def recv(self):
        """Receives the next packet (or *frame*) from the underlying stream.
        This method will block until the packet has been read completely

        :returns: string of data
        """
        return self.__rapid_io_context.recv()

    def send(self, data):
        """Sends the given string of data as a packet over the underlying
        stream. Blocks until the packet has been sent.

        :param data: the byte string to send as a packet
        """
        return self.__rapid_io_context.send(data)

    class __RapidIoContext(object):
        @property
        def raw_handler(self): return self.__raw_handler
        @raw_handler.setter
        def raw_handler(self, value): self.__raw_handler = value

        def raw_write(self, data):
            __data = memoryview(data).cast("B")
            self.__channel.stream.write(self.__channel.FRAME_HEADER.pack(__data.nbytes, self.__Codes.RAW))
            self.__channel.stream.write(data)
            self.__channel.stream.write(self.__channel.FLUSHER)

        def recv(self):
            try:
                __header = self.__channel.stream.read(self.__FRAME_HEADER_SIZE)
                __length, __code = self.__channel.FRAME_HEADER.unpack(__header)
                __result = self.__recv_handlers_dictionary[__code](self, __length)
                __flusher = self.__channel.stream.read(self.__FLUSHER_SIZE)
                if self.__channel.FLUSHER != __flusher: raise RuntimeError("broken stream, invalid flusher received")
            except:
                try: self.__channel.close()
                except: pass
                raise
            return __result

        def send(self, brine_dump_result):
            try:
              if self.__try_send_compressed(brine_dump_result): return
              __size = brine_dump_result.size
              self.__channel.stream.write(self.__channel.FRAME_HEADER.pack(__size, self.__Codes.NORMAL))
              self.__write_data_sequence_to_stream(__size, brine_dump_result.generator, self.__channel.stream)
              self.__channel.stream.write(self.__channel.FLUSHER)
            except:
                try: self.__channel.close()
                except: pass
                raise

        def __init__(self, channel):
            object.__init__(self)
            self.__channel = channel
            self.__raw_handler = None
            self.__raw_read_mutex = threading.Lock()
            self.__FLUSHER_SIZE = len(channel.FLUSHER)
            self.__FRAME_HEADER_SIZE = channel.FRAME_HEADER.size

        class __Codes(object):
            RAW = int((b"r").hex(), 16)
            NORMAL = int((b"n").hex(), 16)
            COMPRESSED = int((b"c").hex(), 16)

        class __BrineLoadStream(object):
            @property
            def counter(self): return self.__counter
            def read(self, size):
                __data = self.__source.read(size)
                self.__counter += len(__data)
                return __data
            def __init__(self, source):
                object.__init__(self)
                self.__source = source
                self.__counter = 0

        class __RawReader(object):
            size = property(lambda self: self.__size)
            left = property(lambda self: self.__left)
            state = property(lambda self: self.__state)
            def skip(self, size = None):
                if size is None: __total = self.__left
                else:
                    if not isinstance(size, int): raise TypeError("invalid size, positive integer expected")
                    if not (0 <= size): raise ValueError("invalid size, positive integer expected")
                    __total = min(self.__left, size)
                if not (0 < __total): return 0
                __left = __total
                try:
                    while 0 < __left:
                        __expected = min(self.__BLACK_HOLE_SIZE, __left)
                        __size = self.__readinto_impl(self.__BLACK_HOLE[:__expected])
                        if __expected != __size: raise RuntimeError("broken stream")
                        __left -= __size
                except: self.__state = False; raise
                self.__left -= __total
                return __total
            def read(self, size):
                if not isinstance(size, int): raise TypeError("invalid size, positive integer expected")
                if 0 == size: return memoryview(bytes())
                if not (0 <= size): raise ValueError("invalid size, positive integer expected")
                if not (0 < self.__left): return memoryview(bytes())
                __expected = min(self.__left, size)
                try:
                    __data = self.__read_impl(__expected)
                    if (__expected != __data.nbytes): raise RuntimeError("broken stream")
                except: self.__state = False; raise
                self.__left -= __expected
                return __data
            def readinto(self, destination):
                __destination = memoryview(destination).cast("B")
                __destination = __destination[:min(self.__left, __destination.nbytes)]
                __expected = __destination.nbytes
                try:
                    __size = self.__readinto_impl(__destination)
                    if __expected != __size: raise RuntimeError("broken stream")
                except: self.__state = False; raise
                self.__left -= __expected
                return __size
            __BLACK_HOLE = memoryview(ctypes.create_string_buffer(1024 * 1024)).cast("B")
            __BLACK_HOLE_SIZE = __BLACK_HOLE.nbytes
            def __readinto_impl(self, destination):
                __destination = destination
                __total = __destination.nbytes
                with self.__mutex:
                    while 0 < __destination.nbytes:
                        __size = self.__stream.readinto(__destination)
                        if 0 == __size: break
                        if not (__size <= __destination.nbytes): raise RuntimeError("broken stream")
                        __destination = __destination[__size:]
                return __total - __destination.nbytes
            def __read_impl(self, size):
                if not (0 < size): return
                __buffer = memoryview(ctypes.create_string_buffer(size)).cast("B")
                self.__readinto_impl(__buffer)
                return __buffer
            def __init__(self, size, stream, mutex):
                object.__init__(self)
                self.__size = size
                self.__left = size
                self.__state = True
                self.__mutex = mutex
                self.__stream = stream

        def __try_send_compressed(self, brine_dump_result):
            if not self.__channel.compression: return False
            __size = brine_dump_result.size
            if not (max(0, self.__channel.COMPRESSION_THRESHOLD) < __size): return False
            with io.BytesIO() as __stream:
              self.__write_data_sequence_to_stream(__size, brine_dump_result.generator, __stream)
              if __size != __stream.tell(): raise ValueError("data size mismatch")
              try: __data = zlib.compress(__stream.getbuffer(), self.__channel.COMPRESSION_LEVEL)
              except: return False
            __size = len(__data)
            self.__channel.stream.write(self.__channel.FRAME_HEADER.pack(__size, self.__Codes.COMPRESSED))
            self.__channel.stream.write(__data)
            self.__channel.stream.write(self.__channel.FLUSHER)
            return True

        @staticmethod
        def __write_data_sequence_to_stream(total, generator, stream):
            __left = total
            for __item in generator:
                __expected = __item.nbytes
                if not (__expected <= __left): raise ValueError("data size mismatch")
                __size = stream.write(__item)
                if (not (__size is None)) and (__size != __expected): raise RuntimeError("broken output stream")
                __left -= __expected
            if 0 != __left: raise ValueError("data size mismatch")

        def __handle_raw_data(self, size):
            __handler = self.__raw_handler
            __raw_reader = self.__RawReader(size, self.__channel.stream, self.__raw_read_mutex)
            if not (__handler is None): __handler(__raw_reader)
            __raw_reader.skip()
            if not __raw_reader.state: raise RuntimeError("broken stream")

        def __handle_normal_data(self, size):
            __stream = self.__BrineLoadStream(self.__channel.stream)
            __data = brine._load(__stream)
            if size != __stream.counter: raise ValueError("broken stream, invalid size mismatch")
            return __data

        def __handle_compressed_data(self, size):
            if not (0 <= size): raise ValueError("broken stream, invalid size field, positive expected")
            with io.BytesIO(zlib.decompress(self.__channel.stream.read(size))) as __stream: return brine._load(__stream)

        __recv_handlers_dictionary = {
            __Codes.RAW: __handle_raw_data,
            __Codes.NORMAL: __handle_normal_data,
            __Codes.COMPRESSED : __handle_compressed_data
        }
