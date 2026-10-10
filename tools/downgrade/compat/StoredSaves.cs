using System;
using UnityEngine;

// Saves written as gzip with stored blocks: SaveCompression, StoredGzipStream.
// Part of DrifterCompat; see Compat2018.cs.
namespace DrifterCompat
{
    // PowerQuest compresses the body of each save with GZipStream (CompressionLevel.Fastest,
    // which the player's zlib ignores: it deflates at the default level). On the PS Vita that
    // is about a quarter of an autosave, which the game makes on entering most rooms. There
    // the body is written as gzip with stored (uncompressed) deflate blocks instead: still
    // gzip, so the game, on any platform, reads it with its own GZipStream; about 0.5-1 MB
    // a save instead of 75-160 KB.
    public static class SaveCompression
    {
        // The game's buffer starts at 32 KB; uncompressed saves reach about 1 MB, which it would
        // reach by doubling and copying.
        const int SaveSize = 1 << 20;

        public static System.IO.Stream Create(System.IO.Stream stream, System.IO.Compression.CompressionLevel level, bool leaveOpen)
        {
            // -compat-stored-saves from the command line only (docs/parity.md).
            if (Application.platform == RuntimePlatform.PSP2 || Platform.OnCommandLine("-compat-stored-saves"))
            {
                var memory = stream as System.IO.MemoryStream;
                if (memory != null && memory.Capacity < SaveSize)
                    try { memory.Capacity = SaveSize; } catch (NotSupportedException) { }
                return new StoredGzipStream(stream, leaveOpen);
            }
            return new System.IO.Compression.GZipStream(stream, level, leaveOpen);
        }
    }

    // Gzip (RFC 1952) whose deflate data is stored blocks (RFC 1951, BTYPE 00), each up to
    // 65535 bytes (its LEN field's limit), with the CRC-32 and size trailer.
    public sealed class StoredGzipStream : System.IO.Stream
    {
        static uint[] table;
        readonly System.IO.Stream inner;
        readonly bool leaveOpen;
        readonly byte[] block = new byte[65535];
        int used;
        uint crc = 0xFFFFFFFF;
        uint length;
        bool finished;

        public StoredGzipStream(System.IO.Stream inner, bool leaveOpen)
        {
            this.inner = inner;
            this.leaveOpen = leaveOpen;
            if (table == null)
            {
                var t = new uint[256];
                for (uint n = 0; n < 256; n++)
                {
                    uint c = n;
                    for (int k = 0; k < 8; k++) c = (c & 1) != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
                    t[n] = c;
                }
                table = t;
            }
            inner.Write(new byte[] { 0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff }, 0, 10);
        }

        public override bool CanRead { get { return false; } }
        public override bool CanSeek { get { return false; } }
        public override bool CanWrite { get { return !finished; } }
        public override long Length { get { throw new NotSupportedException(); } }
        public override long Position { get { throw new NotSupportedException(); } set { throw new NotSupportedException(); } }
        public override int Read(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
        public override long Seek(long offset, System.IO.SeekOrigin origin) { throw new NotSupportedException(); }
        public override void SetLength(long value) { throw new NotSupportedException(); }

        public override void Write(byte[] buffer, int offset, int count)
        {
            if (finished) throw new ObjectDisposedException("StoredGzipStream");
            uint c = crc;
            for (int i = offset, end = offset + count; i < end; i++) c = table[(c ^ buffer[i]) & 0xFF] ^ (c >> 8);
            crc = c;
            length += (uint)count;
            while (count > 0)
            {
                int take = Math.Min(count, block.Length - used);
                Buffer.BlockCopy(buffer, offset, block, used, take);
                used += take;
                offset += take;
                count -= take;
                if (used == block.Length) WriteBlock(false);
            }
        }

        void WriteBlock(bool final)
        {
            inner.Write(new byte[] { (byte)(final ? 1 : 0), (byte)used, (byte)(used >> 8), (byte)~used, (byte)(~used >> 8) }, 0, 5);
            inner.Write(block, 0, used);
            used = 0;
        }

        public override void Flush()
        {
            if (!finished && used > 0) WriteBlock(false);
            inner.Flush();
        }

        protected override void Dispose(bool disposing)
        {
            try
            {
                if (disposing && !finished)
                {
                    finished = true;
                    WriteBlock(true);
                    uint c = crc ^ 0xFFFFFFFF;
                    inner.Write(new byte[] { (byte)c, (byte)(c >> 8), (byte)(c >> 16), (byte)(c >> 24),
                                             (byte)length, (byte)(length >> 8), (byte)(length >> 16), (byte)(length >> 24) }, 0, 8);
                    if (!leaveOpen) inner.Close();
                }
            }
            finally { base.Dispose(disposing); }
        }
    }
}
