import {
  constants,
  createReadStream,
  createWriteStream,
  type ReadStream,
  type Stats,
} from "node:fs";
import {
  lstat,
  mkdtemp,
  open,
  realpath,
  rm,
  type FileHandle,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { createHash } from "node:crypto";
import { isAbsolute, join, relative, sep } from "node:path";
import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import { listPackage, statFile, uncache } from "@electron/asar";

import {
  ArtifactReaderFailure,
  copyArtifactEntry,
  sameArtifactEntry,
  type ArtifactEntry,
  type ArtifactReader,
} from "./ArtifactReader.js";
import { closeAsarHandle, readValidatedAsarEntry } from "./AsarEntryStream.js";
import { admitAsarHeader } from "./AsarHeader.js";
import { OwnedFileHandle } from "../filesystem/OwnedFileHandle.js";
import { readFileHandleChunks } from "../filesystem/readFileHandleChunks.js";

/**
 * Official Electron ASAR adapter with range-streamed member reads.
 *
 * An entry marked `unpacked` is metadata, not an integrity exemption: Electron
 * stores its bytes beside the archive in `<archive>.unpacked`, and callers must
 * hash those companion bytes against the archive's declared integrity value.
 */
export class AsarArtifactReader implements ArtifactReader {
  readonly format = "asar" as const;
  #archiveSize: number | undefined;
  #headerSize: number | undefined;
  readonly #entries = new Map<string, AsarEntryState>();
  readonly #unpackedHandles = new Map<
    UnpackedFileHandle,
    {
      readonly path: string;
      readonly owner: OwnedFileHandle<UnpackedFileHandle>;
    }
  >();
  #snapshot: { readonly path: string; readonly sha256: string } | undefined;
  #snapshotRoot: string | undefined;
  #snapshotOperation: Promise<void> = Promise.resolve();

  constructor(
    private readonly path: string,
    private readonly openUnpackedFile: OpenAsarUnpackedFile = open,
    private readonly removeSnapshot: (path: string) => Promise<void> = (path) =>
      rm(path, { recursive: true, force: true }),
  ) {}

  async *entries(signal?: AbortSignal): AsyncIterable<ArtifactEntry> {
    let paths: string[];
    try {
      await this.prepareContainer(undefined, signal);
      const snapshotPath = this.#snapshotPath();
      this.#resetArchiveState();
      this.#entries.clear();
      const headerHandle = await open(
        snapshotPath,
        constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
      );
      try {
        const admitted = await admitAsarHeader(headerHandle);
        this.#archiveSize = admitted.archiveBytes;
        this.#headerSize = admitted.headerBytes;
      } finally {
        await headerHandle.close();
      }
      abortIfNeeded(signal);
      paths = listPackage(snapshotPath, { isPack: false }).sort((left, right) =>
        left.localeCompare(right, "en"),
      );
      const archiveMetadata = await lstat(snapshotPath);
      if (!archiveMetadata.isFile() || archiveMetadata.isSymbolicLink())
        throw new ArtifactReaderFailure(
          "format",
          "ASAR container is not a regular file",
        );
      this.#archiveSize = archiveMetadata.size;
    } catch (cause: unknown) {
      throw asarFailure(this.path, "inventory", cause);
    }
    for (const listed of paths) {
      abortIfNeeded(signal);
      // @electron/asar returns paths assembled with the host path module.
      // Keep that spelling for its API calls, but expose archive paths with
      // portable separators. On POSIX, backslashes can be literal filename
      // characters and must remain untouched.
      const path = toArtifactPath(listed);
      if (path.length === 0) continue;
      const providerPath = listed.startsWith(sep)
        ? listed.slice(sep.length)
        : listed;
      let metadata: ReturnType<typeof statFile>;
      try {
        metadata = statFile(this.#snapshotPath(), providerPath, false);
      } catch (cause: unknown) {
        throw asarFailure(this.path, `stat ${path}`, cause);
      }
      const kind =
        "files" in metadata
          ? "directory"
          : "link" in metadata
            ? "symlink"
            : "file";
      const entry: ArtifactEntry = {
        path,
        kind,
        declaredSize: "size" in metadata ? metadata.size : null,
        compressedSize: null,
        executable: "executable" in metadata && metadata.executable,
        encrypted: false,
        byteOffset: null,
        declaredSha256:
          "integrity" in metadata &&
          metadata.integrity.algorithm === "SHA256" &&
          /^[a-f0-9]{64}$/u.test(metadata.integrity.hash)
            ? metadata.integrity.hash
            : null,
        unpacked: "unpacked" in metadata && metadata.unpacked === true,
        limitations:
          kind === "symlink"
            ? ["ASAR symlink target was not followed or disclosed."]
            : [],
        adapterKey: providerPath,
      };
      if (kind === "file" && isAsarFileMetadata(metadata))
        this.#entries.set(providerPath, {
          metadata,
          entry: copyArtifactEntry(entry),
        });
      yield entry;
    }
  }

  async open(entry: ArtifactEntry, signal?: AbortSignal): Promise<Readable> {
    abortIfNeeded(signal);
    const state = this.#entries.get(entry.adapterKey);
    if (state === undefined && entry.kind !== "file")
      throw new ArtifactReaderFailure(
        "format",
        "ASAR entry is not a regular file",
      );
    if (state === undefined)
      throw new ArtifactReaderFailure(
        "integrity",
        `ASAR entry was not produced by this reader: ${entry.path}`,
      );
    if (!sameArtifactEntry(entry, state.entry))
      throw new ArtifactReaderFailure(
        "integrity",
        `ASAR entry metadata changed since inventory: ${state.entry.path}`,
      );
    const { metadata } = state;
    const producedEntry = state.entry;
    if (producedEntry.unpacked)
      return this.#openUnpackedStream(producedEntry, signal);
    const archiveSize = this.#archiveSize;
    const headerSize = this.#headerSize;
    const offset =
      metadata.offset === undefined
        ? undefined
        : parseArchiveOffset(metadata.offset);
    const start =
      offset === undefined || headerSize === undefined
        ? undefined
        : 8 + headerSize + offset;
    if (
      archiveSize === undefined ||
      start === undefined ||
      !Number.isSafeInteger(start) ||
      metadata.size < 0 ||
      !Number.isSafeInteger(metadata.size) ||
      start > archiveSize ||
      metadata.size > archiveSize - start
    )
      throw new ArtifactReaderFailure(
        "format",
        `ASAR entry range is outside its container: ${producedEntry.path}`,
      );
    let handle: FileHandle | undefined;
    try {
      const openedHandle = await open(
        this.#snapshotPath(),
        constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
      );
      handle = openedHandle;
      const observed = await openedHandle.stat();
      abortIfNeeded(signal);
      if (!observed.isFile() || observed.size !== archiveSize)
        throw new ArtifactReaderFailure(
          "integrity",
          `ASAR container changed before read: ${producedEntry.path}`,
        );
      if (metadata.size === 0) {
        await closeAsarHandle(() => openedHandle.close(), signal);
        handle = undefined;
        return Readable.from([]);
      }
      const source = openedHandle.createReadStream({
        start,
        end: start + metadata.size - 1,
        autoClose: true,
      });
      return readValidatedAsarEntry(
        source,
        metadata.size,
        producedEntry.path,
        this.path,
        signal,
      );
    } catch (cause: unknown) {
      await handle?.close().catch(() => undefined);
      throw asarFailure(this.path, `read ${producedEntry.path}`, cause);
    }
  }

  /** Capture once per reader lifetime; bind all interpretation to these exact bytes. */
  async prepareContainer(
    expectedSha256?: string,
    signal?: AbortSignal,
  ): Promise<void> {
    await this.#withSnapshotOperation(async () => {
      abortIfNeeded(signal);
      if (this.#snapshot === undefined && this.#snapshotRoot !== undefined)
        await this.#closeSnapshot();
      if (this.#snapshot === undefined) await this.#captureSnapshot(signal);
      if (
        expectedSha256 !== undefined &&
        this.#snapshot?.sha256 !== expectedSha256
      )
        throw new ArtifactReaderFailure(
          "integrity",
          `ASAR container changed before interpretation: ${this.path}`,
        );
    }, signal);
  }

  /** Read the same captured container that supplies headers and packed members. */
  async openContainer(signal?: AbortSignal): Promise<ReadStream> {
    await this.prepareContainer(undefined, signal);
    return createReadStream(
      this.#snapshotPath(),
      signal === undefined ? {} : { signal },
    );
  }

  async close(): Promise<void> {
    await this.#withSnapshotOperation(async () => {
      let cleanupFailure: ArtifactReaderFailure | undefined;
      for (const [handle, { path }] of this.#unpackedHandles) {
        try {
          await this.#closeUnpackedHandle(handle);
        } catch (cause: unknown) {
          cleanupFailure = ArtifactReaderFailure.withCleanup(
            cleanupFailure ?? cause,
            ArtifactReaderFailure.cleanupObservation(cause, path),
          );
        }
      }
      try {
        await this.#closeSnapshot();
      } catch (cause: unknown) {
        cleanupFailure = ArtifactReaderFailure.withCleanup(
          cleanupFailure ?? cause,
          ArtifactReaderFailure.cleanupObservation(cause, this.path),
        );
      }
      if (cleanupFailure !== undefined) throw cleanupFailure;
    });
  }

  async #captureSnapshot(signal?: AbortSignal): Promise<void> {
    try {
      this.#snapshotRoot = await mkdtemp(join(tmpdir(), "rea-asar-snapshot-"));
      const snapshotPath = join(this.#snapshotRoot, "container.asar");
      const hash = createHash("sha256");
      const handle = await open(
        this.path,
        constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
      );
      const source = handle.createReadStream();
      const digesting = new Transform({
        transform(chunk: Buffer, _encoding, callback) {
          hash.update(chunk);
          callback(null, chunk);
        },
      });
      await pipeline(
        source,
        digesting,
        createWriteStream(snapshotPath, { flags: "wx", mode: 0o600 }),
        signal === undefined ? {} : { signal },
      );
      this.#snapshot = { path: snapshotPath, sha256: hash.digest("hex") };
    } catch (cause: unknown) {
      const primary =
        signal?.aborted === true
          ? new ArtifactReaderFailure("cancelled", "ASAR operation cancelled", {
              cause,
            })
          : asarFailure(this.path, "snapshot", cause);
      try {
        await this.#closeSnapshot();
      } catch (cleanupCause: unknown) {
        throw ArtifactReaderFailure.withCleanup(
          primary,
          ArtifactReaderFailure.cleanupObservation(
            cleanupCause,
            this.#snapshotRoot ?? this.path,
          ),
        );
      }
      throw primary;
    }
  }

  async #closeSnapshot(): Promise<void> {
    const root = this.#snapshotRoot;
    if (root !== undefined) {
      try {
        await this.removeSnapshot(root);
        if (this.#snapshotRoot === root) this.#snapshotRoot = undefined;
      } catch (cause: unknown) {
        throw ArtifactReaderFailure.withCleanup(cause, {
          reason: `Could not remove ASAR snapshot: ${root}`,
          resources: [root],
        });
      }
    }
    if (root === undefined || this.#snapshotRoot === undefined) {
      if (this.#snapshot !== undefined) uncache(this.#snapshot.path);
      this.#snapshot = undefined;
      this.#resetArchiveState();
      this.#entries.clear();
    }
  }

  async #withSnapshotOperation<Value>(
    operation: () => Promise<Value>,
    signal?: AbortSignal,
  ): Promise<Value> {
    const previous = this.#snapshotOperation;
    let release!: () => void;
    this.#snapshotOperation = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      await waitForOperation(previous, signal);
    } catch (cause: unknown) {
      void previous.then(release, release);
      throw cause;
    }
    try {
      abortIfNeeded(signal);
      return await operation();
    } finally {
      release();
    }
  }

  #snapshotPath(): string {
    if (this.#snapshot === undefined)
      throw new ArtifactReaderFailure(
        "integrity",
        "ASAR snapshot is not available",
      );
    return this.#snapshot.path;
  }

  provenance(): readonly [] {
    return [];
  }

  async #openUnpackedStream(
    entry: ArtifactEntry,
    signal?: AbortSignal,
  ): Promise<Readable> {
    const unpackedRoot = `${this.path}.unpacked`;
    let handle: UnpackedFileHandle | undefined;
    try {
      const rootMetadata = await lstat(unpackedRoot);
      if (!rootMetadata.isDirectory() || rootMetadata.isSymbolicLink())
        throw new ArtifactReaderFailure(
          "path",
          "ASAR unpacked companion is not a regular directory",
        );
      const canonicalRoot = await realpath(unpackedRoot);
      const candidate = join(unpackedRoot, entry.adapterKey);
      const canonical = await realpath(candidate);
      const relativePath = relative(canonicalRoot, canonical);
      if (
        relativePath === ".." ||
        relativePath.startsWith(`..${sep}`) ||
        isAbsolute(relativePath)
      )
        throw new ArtifactReaderFailure(
          "path",
          `ASAR unpacked entry escaped its companion directory: ${entry.path}`,
        );
      const pathMetadata = await lstat(candidate);
      if (!pathMetadata.isFile() || pathMetadata.isSymbolicLink())
        throw new ArtifactReaderFailure(
          "path",
          `ASAR unpacked entry is not a regular file: ${entry.path}`,
        );
      handle = await this.openUnpackedFile(
        canonical,
        constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
      );
      const openedHandle = handle;
      this.#unpackedHandles.set(openedHandle, {
        path: canonical,
        owner: new OwnedFileHandle(openedHandle),
      });
      const openedMetadata = await openedHandle.stat();
      if (
        !openedMetadata.isFile() ||
        openedMetadata.dev !== pathMetadata.dev ||
        openedMetadata.ino !== pathMetadata.ino
      ) {
        throw new ArtifactReaderFailure(
          "integrity",
          `ASAR unpacked entry changed before read: ${entry.path}`,
          {},
          {
            logicalPath: entry.path,
            declaredSha256: entry.declaredSha256,
            calculatedSha256: null,
            unpacked: true,
          },
        );
      }
      abortIfNeeded(signal);
      const source = readFileHandleChunks(openedHandle, {
        start: 0,
      });
      source.once("close", () => {
        void this.#closeUnpackedHandle(openedHandle).catch(() => undefined);
      });
      return readValidatedAsarEntry(
        source,
        undefined,
        entry.path,
        this.path,
        signal,
      );
    } catch (cause: unknown) {
      let failure = asarUnpackedEntryFailure(this.path, entry, cause);
      if (handle !== undefined && this.#unpackedHandles.has(handle)) {
        try {
          await this.#closeUnpackedHandle(handle);
        } catch (cleanupCause: unknown) {
          failure = ArtifactReaderFailure.withCleanup(
            failure,
            ArtifactReaderFailure.cleanupObservation(
              cleanupCause,
              this.#unpackedHandles.get(handle)?.path ?? entry.path,
            ),
          );
        }
      }
      throw failure;
    }
  }

  async #closeUnpackedHandle(handle: UnpackedFileHandle): Promise<void> {
    const owned = this.#unpackedHandles.get(handle);
    if (owned === undefined) return;
    await owned.owner.close();
    this.#unpackedHandles.delete(handle);
  }

  #resetArchiveState(): void {
    this.#archiveSize = undefined;
    this.#headerSize = undefined;
  }
}

type AsarFileMetadata = Extract<ReturnType<typeof statFile>, { size: number }>;
type AsarEntryState = {
  readonly metadata: AsarFileMetadata;
  readonly entry: ArtifactEntry;
};
type UnpackedFileHandle = {
  readonly fd: number;
  stat(): Promise<Stats>;
  close(): Promise<void>;
  read: FileHandle["read"];
};
type OpenAsarUnpackedFile = (
  path: string,
  flags: number,
) => Promise<UnpackedFileHandle>;

const asarUnpackedEntryFailure = (
  archivePath: string,
  entry: ArtifactEntry,
  cause: unknown,
): ArtifactReaderFailure => {
  if (cause instanceof ArtifactReaderFailure) return cause;
  if (entry.unpacked && isMissingFile(cause))
    return new ArtifactReaderFailure(
      "unavailable",
      `ASAR unpacked entry bytes are unavailable: ${entry.path}`,
      { cause },
      {
        logicalPath: entry.path,
        declaredSha256: entry.declaredSha256,
        calculatedSha256: null,
        unpacked: true,
      },
    );
  return asarFailure(archivePath, `read ${entry.path}`, cause);
};

const isAsarFileMetadata = (
  metadata: ReturnType<typeof statFile>,
): metadata is AsarFileMetadata =>
  "size" in metadata &&
  typeof metadata.size === "number" &&
  !("files" in metadata) &&
  !("link" in metadata);

const parseArchiveOffset = (value: string): number | undefined => {
  if (!/^(?:0|[1-9][0-9]*)$/u.test(value)) return undefined;
  const offset = Number(value);
  return Number.isSafeInteger(offset) ? offset : undefined;
};

const abortIfNeeded = (signal?: AbortSignal): void => {
  if (signal?.aborted === true)
    throw new ArtifactReaderFailure("cancelled", "ASAR operation cancelled");
};

const waitForOperation = async (
  operation: Promise<void>,
  signal?: AbortSignal,
): Promise<void> => {
  if (signal === undefined) return operation;
  abortIfNeeded(signal);
  await new Promise<void>((resolve, reject) => {
    const removeAbortListener = (): void =>
      signal.removeEventListener("abort", onAbort);
    const onAbort = (): void => {
      removeAbortListener();
      reject(
        new ArtifactReaderFailure("cancelled", "ASAR operation cancelled"),
      );
    };
    signal.addEventListener("abort", onAbort, { once: true });
    operation.then(
      () => {
        removeAbortListener();
        resolve();
      },
      (cause: unknown) => {
        removeAbortListener();
        reject(cause);
      },
    );
  });
};

const toArtifactPath = (listed: string): string => {
  const portable = sep === "\\" ? listed.replaceAll("\\", "/") : listed;
  return portable.replace(/^\/+|\/+$/gu, "");
};

const asarFailure = (
  path: string,
  operation: string,
  cause: unknown,
): ArtifactReaderFailure => {
  if (cause instanceof ArtifactReaderFailure) return cause;
  if (isFilesystemFailure(cause))
    return new ArtifactReaderFailure(
      "io",
      `Could not ${operation} ASAR at ${path}: ${cause.message}`,
      { cause },
    );
  return new ArtifactReaderFailure(
    "format",
    `Malformed ASAR during ${operation}: ${path}`,
    { cause },
  );
};

const isFilesystemFailure = (
  cause: unknown,
): cause is NodeJS.ErrnoException & Error =>
  cause instanceof Error &&
  "errno" in cause &&
  typeof cause.errno === "number" &&
  "syscall" in cause &&
  typeof cause.syscall === "string";

const isMissingFile = (cause: unknown): boolean =>
  typeof cause === "object" &&
  cause !== null &&
  "code" in cause &&
  cause.code === "ENOENT";
