"""Portable schedule discovery, evaluation, rendering, and vendor execution.

This module deliberately keeps host scheduler mutation outside the portable
core. It defines the safe, product-neutral data consumed by the cron and Task
Scheduler adapters.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_config_bridge.models import BridgeConfig, Product, TargetConfig
from agent_config_bridge.path_safety import is_directory_reparse_point

__all__ = [
    "CronExpression",
    "ScheduleCatalog",
    "ScheduleDefinition",
    "ScheduleError",
    "ScheduleExecutionError",
    "TargetScheduleSnapshot",
    "VendorExecutionResult",
    "VendorInvocation",
    "build_vendor_invocation",
    "discover_schedules",
    "execute_vendor_invocation",
    "parse_cron",
    "render_target_snapshot",
    "render_target_snapshots",
    "snapshot_is_due",
]

_SCHEMA_VERSION = 1
_SCHEDULE_KEYS = frozenset({"schema_version", "cron", "timezone", "working_directory", "timeout_seconds"})
_REQUIRED_SCHEDULE_KEYS = frozenset({"schema_version", "cron", "timezone", "working_directory"})
_SCHEDULE_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"\\|?*')
_MAX_SCHEDULE_BYTES = 64 * 1024
_MAX_PROMPT_BYTES = 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS = 1_800
_MAX_TIMEOUT_SECONDS = 86_400
_DEFAULT_SEARCH_MINUTES = 8 * 366 * 24 * 60


class ScheduleError(ValueError):
    """Raised when a schedule definition or operation is unsafe or invalid."""


class ScheduleExecutionError(RuntimeError):
    """Raised when a vendor process cannot complete successfully."""


@dataclass(frozen=True, slots=True)
class CronExpression:
    """A parsed five-field numeric Vixie cron expression."""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_unrestricted: bool
    day_of_week_unrestricted: bool

    def matches(self, moment: datetime, timezone: str | tzinfo) -> bool:
        """Return whether an aware instant matches in ``timezone``.

        The UTC round-trip makes imaginary local datetimes in a spring-forward
        gap normalize to a real instant.  Repeated fall-back minutes remain two
        distinct instants and can therefore both match.

        Args:
            moment: An aware datetime representing the instant to evaluate.
            timezone: An IANA timezone key or an already resolved ``ZoneInfo``.

        Raises:
            ScheduleError: If ``moment`` is naive or the timezone is invalid.
        """

        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ScheduleError("cron evaluation requires an aware datetime")
        zone = _coerce_timezone(timezone)
        local = moment.astimezone(UTC).astimezone(zone)

        if local.minute not in self.minutes or local.hour not in self.hours or local.month not in self.months:
            return False

        day_of_month_matches = local.day in self.days_of_month
        vixie_day_of_week = (local.weekday() + 1) % 7
        day_of_week_matches = vixie_day_of_week in self.days_of_week
        if self.day_of_month_unrestricted:
            return day_of_week_matches
        if self.day_of_week_unrestricted:
            return day_of_month_matches
        return day_of_month_matches or day_of_week_matches

    def next_after(
        self,
        moment: datetime,
        timezone: str | tzinfo,
        *,
        search_minutes: int = _DEFAULT_SEARCH_MINUTES,
    ) -> datetime:
        """Return the first matching local datetime strictly after ``moment``.

        Iterating real UTC minutes, rather than naive wall-clock minutes,
        handles skipped and repeated DST minutes without constructing imaginary
        local instants.

        Args:
            moment: An aware datetime after which to search.
            timezone: An IANA timezone key or resolved ``ZoneInfo``.
            search_minutes: Positive upper bound for the search.

        Raises:
            ScheduleError: If inputs are invalid or no match is found within
                the bounded search.
        """

        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ScheduleError("cron evaluation requires an aware datetime")
        if type(search_minutes) is not int or search_minutes <= 0:
            raise ScheduleError("search_minutes must be a positive integer")

        zone = _coerce_timezone(timezone)
        instant = moment.astimezone(UTC)
        cursor = instant.replace(second=0, microsecond=0)
        if cursor <= instant:
            cursor += timedelta(minutes=1)

        for _ in range(search_minutes):
            if self.matches(cursor, zone):
                return cursor.astimezone(zone)
            cursor += timedelta(minutes=1)
        raise ScheduleError(f"cron expression has no match within {search_minutes} minutes: {self.source!r}")


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    """A strict canonical schedule and its exact prompt text."""

    name: str
    source: Path
    cron_expression: CronExpression
    timezone: str
    working_directory: PurePosixPath
    timeout_seconds: int
    prompt: str


@dataclass(frozen=True, slots=True)
class ScheduleCatalog:
    """Deterministically ordered schedules discovered below one catalog."""

    root: Path
    schedules: tuple[ScheduleDefinition, ...]


@dataclass(frozen=True, slots=True)
class TargetScheduleSnapshot:
    """Immutable per-target data suitable for later scheduler rendering."""

    schema_version: int
    schedule_name: str
    target_name: str
    product: Product
    user_home: Path
    cron: str
    timezone: str
    working_directory: Path
    timeout_seconds: int
    prompt: str


@dataclass(frozen=True, slots=True)
class VendorInvocation:
    """A shell-free vendor invocation with prompt text kept on standard input."""

    schedule_name: str
    target_name: str
    argv: tuple[str, ...]
    user_home: Path
    working_directory: Path
    environment: tuple[tuple[str, str], ...] | None
    stdin: str
    timeout_seconds: int

    @property
    def env(self) -> Mapping[str, str] | None:
        """Return an immutable view of the explicit environment, if any."""

        if self.environment is None:
            return None
        return MappingProxyType(dict(self.environment))


@dataclass(frozen=True, slots=True)
class VendorExecutionResult:
    """Captured output from a successful vendor invocation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def parse_cron(value: str) -> CronExpression:
    """Parse a strict five-field numeric Vixie cron expression.

    Supported field grammar consists of wildcards, numbers, inclusive ranges,
    wildcard/range steps, and comma-separated lists.  Names and extensions such
    as ``L``, ``W``, and ``?`` are intentionally rejected.

    Args:
        value: Cron text in minute, hour, day-of-month, month, day-of-week order.

    Returns:
        An immutable parsed cron expression.

    Raises:
        ScheduleError: If the expression or any field is invalid.
    """

    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("cron must be a non-empty string")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ScheduleError("cron must be a single line without null bytes")
    fields = value.split()
    if len(fields) != 5:
        raise ScheduleError(f"cron must contain exactly five fields, found {len(fields)}")

    minute = _parse_cron_field(fields[0], minimum=0, maximum=59, label="minute")
    hour = _parse_cron_field(fields[1], minimum=0, maximum=23, label="hour")
    day_of_month = _parse_cron_field(fields[2], minimum=1, maximum=31, label="day-of-month")
    month = _parse_cron_field(fields[3], minimum=1, maximum=12, label="month")
    day_of_week_raw = _parse_cron_field(fields[4], minimum=0, maximum=7, label="day-of-week")
    days_of_week = frozenset(0 if value == 7 else value for value in day_of_week_raw)

    return CronExpression(
        source=" ".join(fields),
        minutes=minute,
        hours=hour,
        days_of_month=day_of_month,
        months=month,
        days_of_week=days_of_week,
        # Vixie cron decides DOM/DOW OR semantics from the original leading
        # wildcard syntax, not from whether expansion happens to cover the
        # field's entire numeric range (for example ``0-6`` is restricted).
        day_of_month_unrestricted=fields[2].startswith("*"),
        day_of_week_unrestricted=fields[4].startswith("*"),
    )


def discover_schedules(catalog: str | os.PathLike[str] | BridgeConfig) -> ScheduleCatalog:
    """Discover strict ``schedule.toml`` plus ``PROMPT.md`` catalog entries.

    Args:
        catalog: A canonical catalog root or a loaded bridge configuration.

    Returns:
        A deterministic immutable schedule catalog.  A missing ``schedules``
        group is treated as an empty catalog.

    Raises:
        ScheduleError: If paths or schedule contents violate the strict schema.
    """

    raw_root = catalog.catalog if isinstance(catalog, BridgeConfig) else Path(catalog)
    root = _resolve_real_directory(raw_root, "catalog")
    group = root / "schedules"
    if not _lexists(group):
        return ScheduleCatalog(root=root, schedules=())
    _reject_directory_link(group, "schedules catalog group")
    group = _resolve_real_directory(group, "schedules catalog group")

    definitions: list[ScheduleDefinition] = []
    portable_names: dict[str, str] = {}
    try:
        entries = sorted(group.iterdir(), key=lambda entry: entry.name)
    except OSError as error:
        raise ScheduleError(f"cannot inspect schedules catalog group {group}: {error}") from error

    for entry in entries:
        if entry.name.startswith("."):
            continue
        _validate_schedule_name(entry.name, entry)
        portable_name = entry.name.casefold()
        if previous := portable_names.get(portable_name):
            raise ScheduleError(
                f"schedule names collide on case-insensitive filesystems: {previous!r} and {entry.name!r}"
            )
        portable_names[portable_name] = entry.name
        _reject_directory_link(entry, "schedule root")
        schedule_root = _resolve_real_directory(entry, "schedule root")
        definitions.append(_load_schedule(schedule_root, entry.name))

    return ScheduleCatalog(root=root, schedules=tuple(definitions))


def render_target_snapshot(schedule: ScheduleDefinition, target: TargetConfig) -> TargetScheduleSnapshot:
    """Resolve one canonical schedule into immutable target-native data.

    Args:
        schedule: Validated canonical schedule.
        target: Enabled product target whose user home contains the work tree.

    Returns:
        A frozen target snapshot with a physically resolved working directory.

    Raises:
        ScheduleError: If the target is disabled or the directory escapes its
            user home, is missing, or is not a real directory.
    """

    if not target.enabled:
        raise ScheduleError(f"cannot render schedule for disabled target {target.name!r}")
    user_home = _resolve_real_directory(target.user_home, f"target {target.name!r} user_home")
    candidate = user_home.joinpath(*schedule.working_directory.parts)
    working_directory = _resolve_contained_directory(candidate, user_home, schedule.name)
    return TargetScheduleSnapshot(
        schema_version=_SCHEMA_VERSION,
        schedule_name=schedule.name,
        target_name=target.name,
        product=target.product,
        user_home=user_home,
        cron=schedule.cron_expression.source,
        timezone=schedule.timezone,
        working_directory=working_directory,
        timeout_seconds=schedule.timeout_seconds,
        prompt=schedule.prompt,
    )


def render_target_snapshots(
    catalog: ScheduleCatalog,
    target: TargetConfig,
) -> tuple[TargetScheduleSnapshot, ...]:
    """Render every discovered schedule for one target in catalog order."""

    return tuple(render_target_snapshot(schedule, target) for schedule in catalog.schedules)


def snapshot_is_due(snapshot: TargetScheduleSnapshot, moment: datetime) -> bool:
    """Return whether a target snapshot is due at an aware instant."""

    return parse_cron(snapshot.cron).matches(moment, snapshot.timezone)


def build_vendor_invocation(
    snapshot: TargetScheduleSnapshot,
    *,
    environment: Mapping[str, str] | None = None,
    vendor_executable: Path | None = None,
) -> VendorInvocation:
    """Build the exact safe argv/env/stdin contract for a target product.

    The prompt is never included in an argument or environment variable.  No
    permission-bypass option is added.  ``None`` preserves the caller's process
    environment; an explicit mapping is copied into an immutable sorted tuple.

    Args:
        snapshot: Previously rendered target schedule data.
        environment: Optional complete process environment supplied locally at
            execution time.  Catalog schedules cannot define environment data.
        vendor_executable: Optional host-resolved absolute product CLI path.

    Returns:
        A frozen shell-free vendor invocation.

    Raises:
        ScheduleError: If snapshot paths or explicit environment values are
            invalid.
    """

    if snapshot.schema_version != _SCHEMA_VERSION:
        raise ScheduleError(
            f"schedule snapshot schema_version must be {_SCHEMA_VERSION}, found {snapshot.schema_version!r}"
        )
    parse_cron(snapshot.cron)
    _coerce_timezone(snapshot.timezone)
    timeout_seconds = _parse_timeout(snapshot.timeout_seconds, snapshot.schedule_name)
    if not snapshot.prompt.strip() or "\x00" in snapshot.prompt:
        raise ScheduleError(f"schedule {snapshot.schedule_name!r} snapshot contains an invalid prompt")

    user_home = _resolve_real_directory(snapshot.user_home, f"target {snapshot.target_name!r} user_home")
    working_directory = _resolve_contained_directory(
        snapshot.working_directory,
        user_home,
        snapshot.schedule_name,
    )
    argv: tuple[str, ...]
    default_command = "codex" if snapshot.product is Product.CODEX else "claude"
    command = default_command
    if vendor_executable is not None:
        if not vendor_executable.is_absolute() or any(
            character in os.fspath(vendor_executable) for character in ("\x00", "\r", "\n")
        ):
            raise ScheduleError("vendor executable must be an absolute path without control characters")
        command = os.fspath(vendor_executable)
    if snapshot.product is Product.CODEX:
        argv = (command, "exec", "--ephemeral", "-C", os.fspath(working_directory), "-")
    elif snapshot.product is Product.CLAUDE_CODE:
        argv = (command, "--print", "--no-session-persistence")
    else:  # pragma: no cover - Product is closed, but fail closed for forged instances.
        raise ScheduleError(f"unsupported schedule product: {snapshot.product!r}")

    explicit_environment = _freeze_environment(environment)
    return VendorInvocation(
        schedule_name=snapshot.schedule_name,
        target_name=snapshot.target_name,
        argv=argv,
        user_home=user_home,
        working_directory=working_directory,
        environment=explicit_environment,
        stdin=snapshot.prompt,
        timeout_seconds=timeout_seconds,
    )


def execute_vendor_invocation(invocation: VendorInvocation) -> VendorExecutionResult:
    """Execute a vendor invocation without a shell and capture its result.

    Args:
        invocation: Immutable invocation created by
            :func:`build_vendor_invocation`.

    Returns:
        Captured standard output and error for a successful process.

    Raises:
        ScheduleExecutionError: If the executable is unavailable, times out, or
            exits non-zero.  Error messages intentionally omit prompt content.
    """

    _validate_vendor_invocation(invocation)
    environment = None if invocation.environment is None else dict(invocation.environment)
    try:
        completed = subprocess.run(  # noqa: S603 - argv is fixed and shell=False.
            invocation.argv,
            input=invocation.stdin,
            text=True,
            encoding="utf-8",
            errors="strict",
            capture_output=True,
            cwd=invocation.working_directory,
            env=environment,
            timeout=invocation.timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ScheduleExecutionError(
            f"schedule {invocation.schedule_name!r} timed out after {invocation.timeout_seconds} seconds"
        ) from error
    except UnicodeError as error:
        raise ScheduleExecutionError(
            f"vendor command for schedule {invocation.schedule_name!r} could not exchange UTF-8 text"
        ) from error
    except OSError as error:
        raise ScheduleExecutionError(
            f"could not start vendor command for schedule {invocation.schedule_name!r}: {error}"
        ) from error

    if completed.returncode != 0:
        raise ScheduleExecutionError(
            f"vendor command for schedule {invocation.schedule_name!r} exited with code {completed.returncode}"
        )
    return VendorExecutionResult(
        argv=invocation.argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _parse_cron_field(value: str, *, minimum: int, maximum: int, label: str) -> frozenset[int]:
    if not value or value.startswith(",") or value.endswith(","):
        raise ScheduleError(f"invalid {label} cron field: {value!r}")

    items = value.split(",")
    if len(items) > 1 and any(item.partition("/")[0] == "*" for item in items):
        raise ScheduleError(f"wildcard {label} item cannot be combined with a list: {value!r}")

    values: set[int] = set()
    for item in items:
        if not item:
            raise ScheduleError(f"invalid {label} cron field: {value!r}")
        base, separator, raw_step = item.partition("/")
        if separator:
            if "/" in raw_step or not raw_step.isdecimal():
                raise ScheduleError(f"invalid {label} step in cron field: {item!r}")
            step = int(raw_step)
            if step <= 0:
                raise ScheduleError(f"{label} step must be positive: {item!r}")
        else:
            step = 1

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, range_separator, end_text = base.partition("-")
            if not range_separator or not start_text.isdecimal() or not end_text.isdecimal():
                raise ScheduleError(f"invalid {label} range in cron field: {item!r}")
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ScheduleError(f"descending {label} ranges are not supported: {item!r}")
        else:
            if separator or not base.isdecimal():
                raise ScheduleError(f"invalid {label} value in cron field: {item!r}")
            start = end = int(base)

        if start < minimum or end > maximum:
            raise ScheduleError(f"{label} value is outside {minimum}..{maximum}: {item!r}")
        values.update(range(start, end + 1, step))

    if not values:
        raise ScheduleError(f"{label} cron field selects no values: {value!r}")
    return frozenset(values)


def _load_schedule(root: Path, name: str) -> ScheduleDefinition:
    expected = {"schedule.toml", "PROMPT.md"}
    try:
        entries = tuple(root.iterdir())
    except OSError as error:
        raise ScheduleError(f"cannot inspect schedule {root}: {error}") from error
    actual = {entry.name for entry in entries}
    if actual != expected:
        found = ", ".join(sorted(actual)) or "nothing"
        raise ScheduleError(f"schedule must contain exactly schedule.toml and PROMPT.md: {root}; found {found}")

    schedule_path = root / "schedule.toml"
    prompt_path = root / "PROMPT.md"
    for path in (schedule_path, prompt_path):
        if path.is_symlink() or not path.is_file():
            raise ScheduleError(f"schedule sources must be real regular files: {path}")

    schedule_bytes = _read_bounded_file(schedule_path, _MAX_SCHEDULE_BYTES, "schedule definition")
    prompt_bytes = _read_bounded_file(prompt_path, _MAX_PROMPT_BYTES, "schedule prompt")
    try:
        document = tomllib.loads(schedule_bytes.decode("utf-8"))
        prompt = prompt_bytes.decode("utf-8")
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ScheduleError(f"invalid schedule source in {root}: {error}") from error

    unknown = sorted(set(document) - _SCHEDULE_KEYS)
    if unknown:
        raise ScheduleError(f"schedule {name!r} contains unknown keys: {', '.join(repr(key) for key in unknown)}")
    missing = sorted(_REQUIRED_SCHEDULE_KEYS - set(document))
    if missing:
        raise ScheduleError(f"schedule {name!r} is missing required keys: {', '.join(missing)}")
    if type(document["schema_version"]) is not int or document["schema_version"] != _SCHEMA_VERSION:
        raise ScheduleError(f"schedule {name!r} schema_version must be integer {_SCHEMA_VERSION}")

    cron_text = _nonempty_string(document["cron"], f"schedule {name!r} cron")
    timezone = _nonempty_string(document["timezone"], f"schedule {name!r} timezone")
    _coerce_timezone(timezone)
    working_directory_text = _nonempty_string(
        document["working_directory"],
        f"schedule {name!r} working_directory",
    )
    working_directory = _parse_relative_working_directory(working_directory_text, name)
    timeout_seconds = _parse_timeout(document.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), name)

    if not prompt.strip():
        raise ScheduleError(f"schedule prompt must not be empty: {prompt_path}")
    if "\x00" in prompt:
        raise ScheduleError(f"schedule prompt contains a null byte: {prompt_path}")

    return ScheduleDefinition(
        name=name,
        source=root,
        cron_expression=parse_cron(cron_text),
        timezone=timezone,
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
        prompt=prompt,
    )


def _parse_relative_working_directory(value: str, schedule_name: str) -> PurePosixPath:
    if "\x00" in value or "\\" in value:
        raise ScheduleError(f"schedule {schedule_name!r} working_directory must use a relative portable POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ScheduleError(f"schedule {schedule_name!r} working_directory must stay beneath target.user_home")
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part != part.rstrip(" ."):
            raise ScheduleError(f"schedule {schedule_name!r} working_directory is not portable to Windows: {part!r}")
        if any(character in _WINDOWS_INVALID_CHARACTERS or ord(character) < 32 for character in part):
            raise ScheduleError(f"schedule {schedule_name!r} working_directory is not portable to Windows: {part!r}")
        if part.rstrip(" .").partition(".")[0].casefold() in _WINDOWS_DEVICE_NAMES:
            raise ScheduleError(f"schedule {schedule_name!r} working_directory uses a Windows reserved name: {part!r}")
    return path


def _parse_timeout(value: object, schedule_name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise ScheduleError(
            f"schedule {schedule_name!r} timeout_seconds must be an integer from 1 to {_MAX_TIMEOUT_SECONDS}"
        )
    return value


def _coerce_timezone(value: str | tzinfo) -> tzinfo:
    if isinstance(value, ZoneInfo) or value is UTC:
        return value
    if isinstance(value, tzinfo):
        raise ScheduleError("timezone objects must be ZoneInfo instances")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ScheduleError("timezone must be a non-empty IANA timezone key")
    if value.startswith(("/", ".")) or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ScheduleError(f"invalid IANA timezone key: {value!r}")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        # CPython on Windows commonly has no system tzdb.  UTC has no
        # transitions, so its standard-library singleton is an exact fallback
        # while all regional IANA keys still fail closed without real data.
        if value == "UTC":
            return UTC
        raise ScheduleError(f"unknown IANA timezone: {value!r}") from error
    except ValueError as error:
        raise ScheduleError(f"unknown IANA timezone: {value!r}") from error


def _freeze_environment(environment: Mapping[str, str] | None) -> tuple[tuple[str, str], ...] | None:
    if environment is None:
        return None
    frozen: list[tuple[str, str]] = []
    for name, value in environment.items():
        if not isinstance(name, str) or not name or "=" in name or "\x00" in name:
            raise ScheduleError(f"invalid environment variable name: {name!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise ScheduleError(f"invalid value for environment variable {name!r}")
        frozen.append((name, value))
    return tuple(sorted(frozen))


def _validate_vendor_invocation(invocation: VendorInvocation) -> None:
    codex_tail = (
        "exec",
        "--ephemeral",
        "-C",
        os.fspath(invocation.working_directory),
        "-",
    )
    claude_tail = ("--print", "--no-session-persistence")
    if invocation.argv[1:] == codex_tail:
        default_command = "codex"
    elif invocation.argv[1:] == claude_tail:
        default_command = "claude"
    else:
        raise ScheduleExecutionError("refusing to execute an unrecognized vendor argv contract")
    command = invocation.argv[0]
    if command != default_command:
        command_path = Path(command)
        if not command_path.is_absolute() or any(character in command for character in ("\x00", "\r", "\n")):
            raise ScheduleExecutionError("refusing to execute a non-absolute vendor command override")
    if not isinstance(invocation.stdin, str) or not invocation.stdin.strip() or "\x00" in invocation.stdin:
        raise ScheduleExecutionError(f"schedule {invocation.schedule_name!r} has invalid standard input")
    if type(invocation.timeout_seconds) is not int or not 1 <= invocation.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise ScheduleExecutionError(f"schedule {invocation.schedule_name!r} has an invalid timeout")
    try:
        user_home = _resolve_real_directory(invocation.user_home, f"target {invocation.target_name!r} user_home")
        working_directory = _resolve_contained_directory(
            invocation.working_directory,
            user_home,
            invocation.schedule_name,
        )
    except ScheduleError as error:
        raise ScheduleExecutionError(
            f"schedule {invocation.schedule_name!r} working directory is no longer safe"
        ) from error
    if working_directory != invocation.working_directory:
        raise ScheduleExecutionError(f"schedule {invocation.schedule_name!r} working directory identity changed")
    if invocation.environment is not None:
        try:
            environment = dict(invocation.environment)
            if len(environment) != len(invocation.environment):
                raise ScheduleError("duplicate environment variable names")
            _freeze_environment(environment)
        except (TypeError, ValueError, ScheduleError) as error:
            raise ScheduleExecutionError(
                f"schedule {invocation.schedule_name!r} has an invalid execution environment"
            ) from error


def _read_bounded_file(path: Path, maximum_bytes: int, label: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > maximum_bytes:
            raise ScheduleError(f"{label} exceeds {maximum_bytes} bytes: {path}")
        contents = path.read_bytes()
        if len(contents) > maximum_bytes:
            raise ScheduleError(f"{label} exceeds {maximum_bytes} bytes: {path}")
        return contents
    except OSError as error:
        raise ScheduleError(f"cannot read {label} {path}: {error}") from error


def _resolve_real_directory(path: Path, label: str) -> Path:
    _reject_directory_link(path, label)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ScheduleError(f"cannot resolve {label} {path}: {error}") from error
    if not resolved.is_dir():
        raise ScheduleError(f"{label} is not a directory: {path}")
    return resolved


def _resolve_contained_directory(candidate: Path, user_home: Path, schedule_name: str) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(user_home)
    except ValueError as error:
        raise ScheduleError(
            f"schedule {schedule_name!r} working_directory escapes target.user_home: {candidate}"
        ) from error
    except (OSError, RuntimeError) as error:
        raise ScheduleError(
            f"cannot resolve schedule {schedule_name!r} working_directory {candidate}: {error}"
        ) from error
    if not resolved.is_dir():
        raise ScheduleError(f"schedule {schedule_name!r} working_directory is not a directory: {candidate}")
    return resolved


def _reject_directory_link(path: Path, label: str) -> None:
    if not _lexists(path):
        raise ScheduleError(f"{label} does not exist: {path}")
    try:
        reparse_point = is_directory_reparse_point(path)
    except OSError as error:
        raise ScheduleError(f"cannot inspect {label} {path}: {error}") from error
    if path.is_symlink() or reparse_point:
        raise ScheduleError(f"{label} must not be a symlink or junction: {path}")


def _validate_schedule_name(name: str, path: Path) -> None:
    if not _SCHEDULE_NAME.fullmatch(name):
        raise ScheduleError(f"schedule name must be lowercase kebab-case: {path}")
    if name.casefold() in _WINDOWS_DEVICE_NAMES:
        raise ScheduleError(f"schedule name is reserved on Windows: {path}")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{label} must be a non-empty string")
    return value


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)
