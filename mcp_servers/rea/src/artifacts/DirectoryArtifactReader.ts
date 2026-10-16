import { constants, type Stats } from "node:fs";
import {
  lstat,
  open,
  opendir,
  realpath,
  type FileHandle,
} from "node:fs/promises";
import { isAbsolute, join, relative, sep } from "node:path";
import { Readable } from "node:stream";

import {
  ArtifactReaderFailure,
  type ArtifactEntry,
  type ArtifactReader,
} from "./ArtifactReader.js";
import { OwnedDirectoryHandle } from "../filesystem/OwnedDirectoryHandle.js";
import { OwnedFileHandle } from "../filesystem/OwnedFileHandle.js";
import { readFileHandleChunks } from "../filesystem/readFileHandleChunks.js";

/** Non-symlink-following directory reader. */
export class DirectoryArtifactReader implements ArtifactReader {
  readonly format = "directory" as const;
  #rootPromise: Promise<string> | undefined;
  readonly #rootPath: string;
  readonly #directories = new Set<OwnedDirectoryHandle>();
  readonly #files = new Map<OwnedFileHandle, string>();

  constructor(root: string) {
    this.#rootPath = root;
  }

  /** Enumerate entries, optionally restricting descent while retaining directories. */
  async *entries(
    signal?: AbortSignal,
    descendInto?: (path: string) => boolean,
  ): AsyncIterable<ArtifactEntry> {
    const root = await this.#resolveRoot();
    const pending = [root];
    while (pending.length > 0) {
      abortIfNeeded(signal);
      const directory = pending.pop();
      if (directory === undefined) break;
      await withDirectoryIoContext("inspect directory", directory, () =>
        assertContainedDirectory(root, directory),
      );
      for await (const entry of this.#readDirectory(
        directory,
        pending,
        signal,
        descendInto,
      ))
        yield entry;
      await withDirectoryIoContext("inspect directory", directory, () =>
        assertContainedDirectory(root, directory),
      );
    }
  }

  async #resolveRoot(): Promise<string> {
    return (this.#rootPromise ??= realpath(this.#rootPath)).catch(
      (cause: unknown) => {
        throw directoryIoFailure(
          "resolve artifact root",
          this.#rootPath,
          cause,
        );
      },
    );
  }

  async *#readDirectory(
    directory: string,
    pending: string[],
    signal?: AbortSignal,
    descendInto?: (path: string) => boolean,
  ): AsyncIterable<ArtifactEntry> {
    const root = await this.#resolveRoot();
    const handle = new OwnedDirectoryHandle(
      await opendir(directory).catch((cause: unknown) => {
        throw directoryIoFailure("open directory", directory, cause);
      }),
    );
    this.#directories.add(handle);
    const childDirectories: string[] = [];
    let traversal:
      | { readonly kind: "complete" }
      | { readonly kind: "failed"; readonly cause: unknown } = {
      kind: "complete",
    };
    try {
      for (;;) {
        const child = await handle.handle.read();
        if (child === null) break;
        abortIfNeeded(signal);
        const absolute = join(directory, child.name);
        const metadata = await lstat(absolute).catch((cause: unknown) => {
          throw directoryIoFailure("inspect entry", absolute, cause);
        });
        const path = relative(root, absolute).split(sep).join("/");
        const entry = artifactEntry(path, absolute, metadata);
        if (entry === undefined) continue;
        yield entry;
        if (entry.kind === "directory" && (descendInto?.(path) ?? true))
          childDirectories.push(absolute);
      }
    } catch (cause: unknown) {
      traversal = {
        kind: "failed",
        cause:
          cause instanceof ArtifactReaderFailure
            ? cause
            : directoryIoFailure("read directory", directory, cause),
      };
    } finally {
      await this.#finishDirectory(handle, directory, traversal);
    }
    if (traversal.kind === "failed") throw traversal.cause;
    childDirectories.reverse();
    pending.push(...childDirectories);
  }

  async #finishDirectory(
    handle: OwnedDirectoryHandle,
    path: string,
    traversal:
      | { readonly kind: "complete" }
      | { readonly kind: "failed"; readonly cause: unknown },
  ): Promise<void> {
    try {
      await handle.close();
      this.#directories.delete(handle);
    } catch (cause: unknown) {
      const closeFailure = directoryIoFailure("close directory", path, cause);
      if (traversal.kind === "failed")
        throw ArtifactReaderFailure.withCleanup(
          traversal.cause,
          ArtifactReaderFailure.cleanupObservation(closeFailure, path),
        );
      throw closeFailure;
    }
  }

  async open(entry: ArtifactEntry, signal?: AbortSignal): Promise<Readable> {
    abortIfNeeded(signal);
    if (entry.kind !== "file")
      throw new ArtifactReaderFailure(
        "format",
        "Only regular files can be opened",
      );
    const fileHandle = await open(
      entry.adapterKey,
      constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
    ).catch((cause: unknown) => {
      throw directoryIoFailure("open entry", entry.adapterKey, cause);
    });
    const owner = new OwnedFileHandle(fileHandle);
    this.#files.set(owner, entry.adapterKey);
    try {
      const observed = await owner.handle.stat().catch((cause: unknown) => {
        throw directoryIoFailure(
          "inspect opened entry",
          entry.adapterKey,
          cause,
        );
      });
      if (
        !observed.isFile() ||
        entry.sourceIdentity === undefined ||
        observed.dev !== entry.sourceIdentity.device ||
        observed.ino !== entry.sourceIdentity.inode
      ) {
        throw new ArtifactReaderFailure(
          "integrity",
          `Directory entry changed before open: ${entry.path}`,
        );
      }
      abortIfNeeded(signal);
      const source = Readable.from(
        mapFileReadCancellation(owner.handle, signal),
        { objectMode: false },
      );
      source.once("close", () => {
        void this.#closeFile(owner).catch(() => undefined);
      });
      return source;
    } catch (cause: unknown) {
      try {
        await this.#closeFile(owner);
      } catch (closeCause: unknown) {
        throw ArtifactReaderFailure.withCleanup(
          cause,
          ArtifactReaderFailure.cleanupObservation(
            directoryIoFailure(
              "close opened entry",
              entry.adapterKey,
              closeCause,
            ),
            entry.adapterKey,
          ),
        );
      }
      throw cause;
    }
  }

  async close(): Promise<void> {
    const failures: unknown[] = [];
    for (const [owner, path] of this.#files) {
      try {
        await this.#closeFile(owner);
      } catch (cause: unknown) {
        failures.push(directoryIoFailure("close opened entry", path, cause));
      }
    }
    for (const owner of this.#directories) {
      try {
        await owner.close();
        this.#directories.delete(owner);
      } catch (cause: unknown) {
        failures.push(
          directoryIoFailure("close directory", owner.handle.path, cause),
        );
      }
    }
    if (failures.length > 0) {
      const reason = failures.map(failureMessage).join("; ");
      throw new ArtifactReaderFailure(
        "io",
        `Directory artifact cleanup failed: ${reason}`,
        {
          cause: new AggregateError(failures),
          cleanup: {
            reason,
            resources: [
              ...this.#files.values(),
              ...[...this.#directories].map((owner) => owner.handle.path),
            ],
          },
        },
      );
    }
  }

  async #closeFile(owner: OwnedFileHandle<FileHandle>): Promise<void> {
    await owner.close();
    this.#files.delete(owner);
  }

  provenance(): readonly [] {
    return [];
  }
}

const artifactEntry = (
  path: string,
  absolute: string,
  metadata: Stats,
): ArtifactEntry | undefined => {
  const kind = metadata.isSymbolicLink()
    ? "symlink"
    : metadata.isDirectory()
      ? "directory"
      : metadata.isFile()
        ? "file"
        : undefined;
  if (kind === undefined) return undefined;
  return {
    path,
    kind,
    declaredSize: kind === "file" ? metadata.size : null,
    compressedSize: null,
    executable: (metadata.mode & 0o111) !== 0,
    encrypted: false,
    byteOffset: null,
    declaredSha256: null,
    unpacked: false,
    limitations:
      kind === "symlink"
        ? ["Symlink target was not followed or disclosed."]
        : [],
    adapterKey: absolute,
    ...(kind === "file"
      ? {
          sourceIdentity: { device: metadata.dev, inode: metadata.ino },
        }
      : {}),
  };
};

const directoryIoFailure = (
  operation: string,
  path: string,
  cause: unknown,
): ArtifactReaderFailure => {
  if (cause instanceof ArtifactReaderFailure) return cause;
  const code =
    typeof cause === "object" &&
    cause !== null &&
    "code" in cause &&
    typeof cause.code === "string"
      ? ` (${cause.code})`
      : "";
  const detail = failureMessage(cause);
  return new ArtifactReaderFailure(
    "io",
    `Could not ${operation} at ${path}${code}: ${detail}`,
    { cause },
  );
};

const failureMessage = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

const mapFileReadCancellation = async function* (
  handle: FileHandle,
  signal?: AbortSignal,
): AsyncGenerator<Buffer> {
  try {
    for await (const chunk of readFileHandleChunks(handle, { signal })) {
      abortIfNeeded(signal);
      yield chunk;
    }
    abortIfNeeded(signal);
  } catch (cause: unknown) {
    if (signal?.aborted === true)
      throw new ArtifactReaderFailure(
        "cancelled",
        "Directory artifact read cancelled",
        { cause },
      );
    throw cause;
  }
};

const withDirectoryIoContext = async <T>(
  operation: string,
  path: string,
  action: () => Promise<T>,
): Promise<T> => {
  try {
    return await action();
  } catch (cause: unknown) {
    throw directoryIoFailure(operation, path, cause);
  }
};

const abortIfNeeded = (signal?: AbortSignal): void => {
  if (signal?.aborted === true)
    throw new ArtifactReaderFailure(
      "cancelled",
      "Artifact traversal cancelled",
    );
};

const assertContainedDirectory = async (
  root: string,
  directory: string,
): Promise<void> => {
  const canonical = await realpath(directory);
  const fromRoot = relative(root, canonical);
  const metadata = await lstat(directory);
  if (
    metadata.isSymbolicLink() ||
    !metadata.isDirectory() ||
    fromRoot === ".." ||
    fromRoot.startsWith(`..${sep}`) ||
    isAbsolute(fromRoot)
  )
    throw new ArtifactReaderFailure(
      "path",
      "Directory traversal escaped or changed its artifact root",
    );
};
