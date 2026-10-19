# ADR-0008: Generation-bound retention capture

## Status

Accepted — 2026-10-19

Phase 1 deletes only POSIX Skill backup directory snapshots. Native Windows,
terminal symlink snapshots, and marketplace builds remain deferred.

## Context

`agentbridge state prune` plans bounded retention for marketplace builds and
Skill backups. Since 0.3.3 an action-bearing `--yes` has failed before mutation.
A reviewed action bound path, kind, byte total, `mtime_ns`, `st_dev`, and
`st_ino`. Each of those can be reused. ext4 reuses inode numbers, `mtime` can be
set, the byte total is only a sum, and `tree_digest` excludes the ownership
marker. A snapshot deleted and then recreated byte-identically on its freed
inode was therefore indistinguishable from the reviewed object. The locked
re-plan also checked by path, and deletion then worked by name, so the named
object could change between the last `lstat` and `rmtree`.

Concurrent Bridge `apply` and `sync-skills` do not take the retention lock.
They write backups in place, may restore from the backup they just made, and
reuse or recreate content-addressed builds by name. Same-account tooling such
as `cp -a`, `rsync`, indexers, and Defender can also hold or recreate entries.
`--yes` plans in-process immediately before applying, so a human review does
not close the gap.

## Decision drivers

- Make the only destructive call act on an object whose identity cannot be
  reused, observed after it can no longer change by name.
- Keep every step before deletion reversible.
- Survive process interruption at any step without deleting an unverified or
  partially recovered object.
- Keep planning strictly read-only and keep the prune JSON at schema 1 with
  additive fields only.
- Prefer deferral over deletion whenever capability is unproven.

## Considered options

### Keep destructive retention disabled

This is safe, but backups grow without bound and the reviewed limits are never
enforced.

### Write a Bridge generation marker into each snapshot

`cp -a` copies the marker. Migrating legacy snapshots has the same ABA problem,
and restore copies markers back into live destinations. A marker identifies
content, not an object.

### Bind the kernel inode generation and capture by rename

`FS_IOC_GETVERSION` returns `i_generation`, which ext4 regenerates when it
reallocates an inode. Renaming the candidate into a private directory removes
it from every name another writer uses before it is verified and deleted.

## Decision

Retention binds each Skill backup directory candidate to `(st_dev, st_ino,
i_generation)`. These are read with `fstat` and `FS_IOC_GETVERSION` on an
`O_DIRECTORY | O_NOFOLLOW` descriptor. It also binds a `capture_digest` and
`entry_count` over every entry, including the ownership marker. The digest
starts with a format tag and length-prefixes each relative path, entry type,
file length plus content, and symlink target, so distinct trees cannot collide
by shifting bytes between fields. The walk is descriptor-relative and classifies
entries with `lstat`, so symlinked directories are recorded as links and never
followed. It rejects any entry whose `st_dev` differs from the candidate root,
and any opened directory or file whose `mnt_id` (from `/proc/self/fdinfo`)
differs from the root's, so a bind mount on the same device is caught too. A
candidate that is itself a mount point is rejected the same way. A rejected
candidate is a planning blocker for the whole plan. `mtime` and `ctime` are not
identity.

Planning defers a candidate, and never deletes it, when any of these holds:

- the platform is native Windows (phase 2 needs a dedicated deleter and a soak
  on the real host);
- the snapshot is a terminal symlink;
- it is a marketplace build (render and publish must share the retention lock
  first);
- it changed within the 15-minute quiescence window (newest `ctime` in the
  tree);
- the capability probe fails.

The probe requires all of the following:

- `os.name == "posix"` and `sys.platform == "linux"`;
- a generic-ioctl machine (`x86_64` or `aarch64`, as `platform.machine()`
  spells them on Linux);
- `shutil.rmtree.avoids_symlink_attacks`;
- descriptor-relative `open`, `stat`, `rename`, `mkdir`, `rmdir`, `unlink`,
  `readlink`, and `scandir`;
- a working `FS_IOC_GETVERSION` and a readable fdinfo `mnt_id` on the actual
  `backups` root;
- an `ext2`, `ext3`, or `ext4` mount for that device according to
  `/proc/self/mountinfo`;
- a private `0700` `retention/` tree, owned by the current user, on the same
  device as the backups, or the state directory's device when the tree is
  absent.

A deferral is not an action and carries a machine-readable `reason_code`:
`marketplace_build`, `symlink_snapshot`, `capture_unavailable`, or
`quiescence`. It never makes `--yes` fail, and `converged` means no
non-deferred actions and no blockers. `fully_converged` additionally requires
no deferrals and no pending recovery.

An apply that returns always ends converged: exit status 0 with
`applied: true` implies `converged: true`. Every apply that ends with deletion
candidates or blockers in its final plan stops with exit status 2,
`applied: false`, and `error`, and still reports what it deleted, restored, or
quarantined. In the JSON, `safe`, `has_changes`, `has_blockers`, the counts,
`actions`, and `blockers` describe the reviewed plan. `converged`,
`fully_converged`, `deferred`, `deferred_bytes`, `recovery_pending`, and
`capture` describe the final plan. `deleted` lists what was actually deleted,
even when `applied` is false.

Under the retention lock, apply first recovers interrupted runs. It then
requires a fresh plan, built with the reviewed quiescence cutoff, to equal the
reviewed plan apart from pending recovery. It creates
`state_dir/retention/capture/<run>/` exclusively and atomically writes
`manifest.json` with every item in state `intent`. For each item it:

1. walks the ancestors one `O_NOFOLLOW` component at a time, and stops if a
   kept sibling snapshot no longer matches its recorded identity, so a group
   never drops below its limit;
2. moves the candidate with `rename(src_dir_fd, dst_dir_fd)` into the run
   directory, and never copies it. On any rename error nothing has changed, and
   the item is reported as `busy`;
3. records `captured`, then verifies the moved object's identity, kind, digest,
   entry count, byte total, and marker source;
4. on mismatch, renames the object back if it is a real directory and its
   original name is free, and otherwise quarantines it under
   `retention/quarantine/<run>/`. It then stops with an error. Only a definitive rename error (`ENOENT`, `ENOTDIR`, `ELOOP`,
   `EEXIST`, `ENOTEMPTY`) leads to quarantine; any other error stops the run and
   leaves the item in the capture run for recovery;
5. on match, re-checks the kept sibling snapshots and restores the capture and
   stops if one changed, so no `verified` record exists for it;
6. records `verified`, re-reads the manifest from disk, requires that item to be
   durably `verified` for this run, re-checks the slot identity, and walks the
   slot again for device and mount boundaries. A mount found there is
   quarantined, never deleted. The same walk gives each directory in the slot
   that lacks owner `rwx` mode `0700` through that directory's own no-follow
   descriptor, never by path, so a read-only subdirectory does not block
   `rmtree`. It then removes the slot with descriptor-based `rmtree` and records
   `deleted`. The result lists the deletion before that record is written, so a
   failed write still reports it. If the delete is refused, the item is recorded
   as `captured` again before the slot is renamed back, so a crash after that
   move is never reported as a deletion.

Recovery handles each interrupted state as follows:

- `intent` or `captured` with a present slot is renamed back or quarantined,
  never deleted. Quarantine needs a definitive error; a transient error opening
  the group or renaming back stops recovery and keeps the run for a retry;
- `verified` finishes the delete and never restores a possibly partial tree;
  if the delete is refused (slot identity changed, or a mount), it is
  quarantined instead;
- an unsettled item whose slot is already in `quarantine/<run>/` is recorded as
  `quarantined`, not `deleted`, because a crash followed the quarantine rename.
  Planning reports that quarantine entry as recovery work, so this recovery can
  run;
- an empty `quarantine/<run>/` left by a crash before the quarantine rename is
  recovery work, and recovery removes it;
- leftovers that cannot be removed yet stay as pending cleanup and are retried;
- unknown entries, a malformed manifest, or a slot still present for an item
  recorded as `deleted`, `restored`, or `quarantined` are planning blockers;
- quarantined entries are persistent blockers until an operator removes them;
- on a host where the probe's platform checks fail, recovery refuses only when
  capture runs exist.

Recovery that quarantines a capture always stops with exit status 2. Recovery
that restores a capture changes the plan; that run deletes nothing new, and it
exits with status 2 only if candidates or blockers remain. Recovery that only
cleans up or finishes verified deletes leaves the plan equal, and the same run
goes on to apply it.

Read-only planning reports interrupted runs as `recovery_pending`, which is not
a blocker; otherwise the CLI would exit before apply and recovery could never
run. Planning never creates `retention/`. Backup naming is unchanged, because
mixing local-time and UTC names would break name-sorted selection.

## Consequences

### Positive

- An ABA replacement, an inode reuse, a byte-identical copy, or a swap inside
  the rename window is restored or quarantined instead of deleted.
- Skill backup growth is bounded on the supported WSL/Linux ext4 layout.
- Every interrupted state has one idempotent recovery path.
- Existing JSON consumers keep schema 1 and the fields they validate.

### Negative

- Marketplace builds, symlink snapshots, and all Windows-native candidates
  still accumulate until later phases.
- Unsupported filesystems (`tmpfs`, `xfs`, `btrfs`, `9p`/drvfs, overlay)
  defer everything until their generation semantics are verified and
  allowlisted.
- A run whose recovery restores a capture deletes nothing new, and it exits with
  status 2 only if candidates or blockers remain. A restored capture's rename
  updates its `ctime`, so it is usually deferred for the quiescence window
  first; a run that restores only that capture therefore exits with status 0
  and lists it in `restored` and `recovered`.
- A directory inside a backup that its owner cannot read is not made readable;
  its capture stays as pending cleanup.
- A mount at or inside a candidate is a planning blocker for the whole plan
  (exit status 1), not `busy`. So is a candidate on a bind-mounted filesystem
  without `FS_IOC_GETVERSION`, because its identity cannot be bound.
- A bind mount between a backup group and the capture directory (the same
  device but a different mount) makes that candidate's capture rename fail with
  `EXDEV`. The item is reported as `busy` and the run stops, but earlier
  candidates in that run may already have been deleted.

### Security consequences

- A generation is an object-identity aid, not authorization. A same-account
  actor that can rewrite or delete Bridge state directly, including the private
  capture directory, remains outside this boundary.
- Capture cannot revoke a descriptor that another same-user process opened
  before the rename. Writes through it reach the captured object and can be
  lost when that object is deleted.
- Only a capture that is a real directory is ever renamed back; any other
  capture is quarantined. The restore checks that the original name is free and
  then renames without `RENAME_NOREPLACE`, so only a directory created empty at
  that name in that instant could be replaced. A non-empty directory makes the
  rename fail with `ENOTEMPTY` and a non-directory with `ENOTDIR`, and the
  capture is then quarantined. Snapshot names carry a random 8-hex suffix, so
  only a deliberate same-account actor can race this. Phase 1 does not call
  `renameat2` through `ctypes` to close it.
- The pre-`rmtree` mount check cannot see a mount created after it. A mount
  visible to Bridge needs mount privilege in Bridge's own mount namespace, so
  that actor is outside this boundary.
- Manifest transitions are fsynced together with their directories, but the
  phase does not claim power-loss durability for `rmtree`, and makes no
  durability claim on Windows.

## Related decisions

- [ADR-0002: Never share runtime state](0002-never-share-runtime-state.md)
- [ADR-0007: Preserve Codex profile Hook trust state](0007-preserve-codex-profile-hook-trust-state.md)

## References

- [Python: `shutil.rmtree.avoids_symlink_attacks`](https://docs.python.org/3/library/shutil.html#shutil.rmtree.avoids_symlink_attacks)
- [Python: paths relative to directory descriptors](https://docs.python.org/3/library/os.html#dir-fd)
- [Linux: `proc_pid_mountinfo(5)`](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)
