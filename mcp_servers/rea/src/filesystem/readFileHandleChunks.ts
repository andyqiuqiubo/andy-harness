import type { FileHandle } from "node:fs/promises";
import { Readable } from "node:stream";

/** Stream positional reads while leaving descriptor cleanup to its owner. */
export const readFileHandleChunks = (
  handle: Pick<FileHandle, "read">,
  options: {
    readonly start?: number;
    readonly end?: number;
    readonly signal?: AbortSignal | undefined;
  } = {},
): Readable => {
  const start = options.start ?? 0;
  const end = options.end ?? Infinity;
  if (
    !Number.isSafeInteger(start) ||
    start < 0 ||
    (end !== Infinity && (!Number.isSafeInteger(end) || end < start))
  )
    throw new RangeError("Invalid file read range");
  return Readable.from(
    (async function* () {
      let position = start;
      while (position <= end) {
        options.signal?.throwIfAborted();
        const chunk = Buffer.allocUnsafe(
          Math.min(64 * 1024, end - position + 1),
        );
        const { bytesRead } = await handle.read(
          chunk,
          0,
          chunk.length,
          position,
        );
        options.signal?.throwIfAborted();
        if (bytesRead === 0) return;
        position += bytesRead;
        yield chunk.subarray(0, bytesRead);
      }
    })(),
    {
      objectMode: false,
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    },
  );
};
