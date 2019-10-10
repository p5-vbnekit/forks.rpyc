"""*Channel* is an abstraction layer over streams that works with *packets of data*,
rather than an endless stream of bytes, and adds support for compression.
"""
from rpyc.lib import safe_import
from rpyc.lib.compat import Struct, BYTES_LITERAL
from rpyc.core import brine
import io
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
            self.__FLUSHER_SIZE = len(channel.FLUSHER)
            self.__FRAME_HEADER_SIZE = channel.FRAME_HEADER.size

        class __Codes(object):
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

        def __handle_normal_data(self, size):
            __stream = self.__BrineLoadStream(self.__channel.stream)
            __data = brine._load(__stream)
            if size != __stream.counter: raise ValueError("broken stream, invalid size mismatch")
            return __data

        def __handle_compressed_data(self, size):
            if not (0 <= size): raise ValueError("broken stream, invalid size field, positive expected")
            with io.BytesIO(zlib.decompress(self.__channel.stream.read(size))) as __stream: return brine._load(__stream)

        __recv_handlers_dictionary = {
            __Codes.NORMAL: __handle_normal_data,
            __Codes.COMPRESSED : __handle_compressed_data
        }
