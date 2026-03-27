# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Date parsing utilities ported from superset/utils/date_parser.py."""
from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from time import struct_time

import pandas as pd
import parsedatetime
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from holidays import country_holidays
from pandas.core.dtypes.common import is_numeric_dtype
from pyparsing import (
    CaselessKeyword,
    Forward,
    Group,
    Optional as ppOptional,
    ParseException,
    ParserElement,
    ParseResults,
    pyparsing_common,
    quotedString,
    Suppress,
)

from superset.exceptions import SupersetValidationException

ParserElement.enable_packrat()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DTTM_ALIAS = "__timestamp"
NO_TIME_RANGE = "No filter"
LRU_CACHE_MAX_SIZE = 256


class InstantTimeComparison(StrEnum):
    INHERITED = "r"
    YEAR = "y"
    MONTH = "m"
    WEEK = "w"


# ---------------------------------------------------------------------------
# Exception classes (replace superset.commands.chart.exceptions)
# ---------------------------------------------------------------------------


class TimeRangeAmbiguousError(SupersetValidationException):
    """Time range is ambiguous error."""

    def __init__(self, human_readable: str) -> None:
        super().__init__(
            message=(
                "Time string is ambiguous."
                f" Please specify [{human_readable} ago]"
                f" or [{human_readable} later]."
            ),
        )


class TimeRangeParseFailError(SupersetValidationException):
    def __init__(self, human_readable: str) -> None:
        super().__init__(
            message=f"Cannot parse time string [{human_readable}]",
        )


class TimeDeltaAmbiguousError(SupersetValidationException):
    """Time delta is ambiguous error."""

    def __init__(self, human_readable: str) -> None:
        super().__init__(
            message=(
                "Time delta is ambiguous."
                f" Please specify [{human_readable} ago]"
                f" or [{human_readable} later]."
            ),
        )


# ---------------------------------------------------------------------------
# DateColumn dataclass (from superset/utils/core.py)
# ---------------------------------------------------------------------------


@dataclass
class DateColumn:
    col_label: str
    timestamp_format: str | None = None
    offset: int | None = None
    time_shift: str | None = None

    def __hash__(self) -> int:
        return hash(self.col_label)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, DateColumn) and hash(self) == hash(other)

    @classmethod
    def get_legacy_time_column(
        cls,
        timestamp_format: str | None,
        offset: int | None,
        time_shift: str | None,
    ) -> DateColumn:
        return cls(
            timestamp_format=timestamp_format,
            offset=offset,
            time_shift=time_shift,
            col_label=DTTM_ALIAS,
        )


# ---------------------------------------------------------------------------
# Core date helpers
# ---------------------------------------------------------------------------


def dttm_from_timetuple(date_: struct_time) -> datetime:
    return datetime(
        date_.tm_year,
        date_.tm_mon,
        date_.tm_mday,
        date_.tm_hour,
        date_.tm_min,
        date_.tm_sec,
    )


def get_past_or_future(
    human_readable: str | None,
    source_time: datetime | None = None,
) -> datetime:
    cal = parsedatetime.Calendar()
    source_dttm = dttm_from_timetuple(
        source_time.timetuple() if source_time else datetime.now().timetuple()
    )
    return dttm_from_timetuple(cal.parse(human_readable or "", source_dttm)[0])


def parse_human_timedelta(
    human_readable: str | None,
    source_time: datetime | None = None,
) -> timedelta:
    """
    Returns ``datetime.timedelta`` from natural language time deltas

    >>> parse_human_timedelta('1 day') == timedelta(days=1)
    True
    """
    source_dttm = dttm_from_timetuple(
        source_time.timetuple() if source_time else datetime.now().timetuple()
    )
    return get_past_or_future(human_readable, source_time) - source_dttm


def normalize_time_delta(human_readable: str) -> dict[str, int]:
    x_unit = (
        r"^\s*([0-9]+)\s+"
        r"(second|minute|hour|day|week|month|quarter|year)s?"
        r"\s+(ago|later)*$"
    )
    matched = re.match(x_unit, human_readable, re.IGNORECASE)
    if not matched:
        raise TimeDeltaAmbiguousError(human_readable)

    key = matched[2] + "s"
    value = int(matched[1])
    value = -value if matched[3] == "ago" else value
    return {key: value}


def parse_human_datetime(human_readable: str) -> datetime:
    """Returns ``datetime.datetime`` from human readable strings"""
    x_periods = (
        r"^\s*([0-9]+)\s+(second|minute|hour|day|week|month|quarter|year)s?\s*$"
    )
    if re.search(x_periods, human_readable, re.IGNORECASE):
        raise TimeRangeAmbiguousError(human_readable)
    try:
        default = datetime(year=datetime.now().year, month=1, day=1)
        dttm = parse(human_readable, default=default)
    except (ValueError, OverflowError) as ex:
        cal = parsedatetime.Calendar()
        parsed_dttm, parsed_flags = cal.parseDT(human_readable)
        # 0 == not parsed at all
        if parsed_flags == 0:
            logger.debug(ex)
            raise TimeRangeParseFailError(human_readable) from ex
        # when time is not extracted, we 'reset to midnight'
        if parsed_flags & 2 == 0:
            parsed_dttm = parsed_dttm.replace(hour=0, minute=0, second=0)
        dttm = dttm_from_timetuple(parsed_dttm.utctimetuple())
    return dttm


def parse_past_timedelta(
    delta_str: str, source_time: datetime | None = None
) -> timedelta:
    """
    Takes a delta like '1 year' and finds the timedelta for that period in
    the past, then represents that past timedelta in positive terms.

    parse_human_timedelta('1 year') find the timedelta 1 year in the future.
    parse_past_timedelta('1 year') returns -datetime.timedelta(-365)
    or datetime.timedelta(365).
    """
    return -parse_human_timedelta(
        delta_str if delta_str.startswith("-") else f"-{delta_str}",
        source_time,
    )


def add_ago_to_since(since: str) -> str:
    """
    Backwards compatibility hack. Without this slices with since: 7 days will
    be treated as 7 days in the future.
    """
    since_words = since.split(" ")
    grains = ["days", "years", "hours", "day", "year", "weeks"]
    if len(since_words) == 2 and since_words[1] in grains:
        since += " ago"
    return since


# ---------------------------------------------------------------------------
# Relative-time helper functions used inside get_since_until
# ---------------------------------------------------------------------------


def get_relative_base(unit: str, relative_start: str | None = None) -> str:
    if relative_start is not None:
        return relative_start

    granular_units = {"second", "minute", "hour"}
    broad_units = {"day", "week", "month", "quarter", "year"}

    if unit.lower() in granular_units:
        return "now"
    elif unit.lower() in broad_units:
        return "today"
    raise ValueError(f"Unknown unit: {unit}")


def handle_start_of(base_expression: str, unit: str) -> str:
    valid_units = {"year", "quarter", "month", "week", "day"}
    if unit in valid_units:
        return f"DATETRUNC({base_expression}, {unit})"
    raise ValueError(f"Invalid unit for 'start of': {unit}")


def handle_end_of(base_expression: str, unit: str) -> str:
    valid_units = {"year", "quarter", "month", "week", "day"}
    if unit in valid_units:
        return f"LASTDAY({base_expression}, {unit})"
    raise ValueError(f"Invalid unit for 'end of': {unit}")


def handle_modifier_and_unit(
    modifier: str, scope: str, delta: str, unit: str, relative_base: str
) -> str:
    base_expression = handle_scope_and_unit(scope, delta, unit, relative_base)

    if modifier.lower() in ["start of", "beginning of"]:
        return handle_start_of(base_expression, unit.lower())
    elif modifier.lower() == "end of":
        return handle_end_of(base_expression, unit.lower())
    else:
        raise ValueError(f"Unknown modifier: {modifier}")


def handle_scope_and_unit(
    scope: str, delta: str, unit: str, relative_base: str
) -> str:
    _delta = int(delta) if delta else 1
    if scope.lower() == "this":
        return f"DATETIME('{relative_base}')"
    elif scope.lower() in ["last", "prior"]:
        return f"DATEADD(DATETIME('{relative_base}'), -{_delta}, {unit})"
    elif scope.lower() == "next":
        return f"DATEADD(DATETIME('{relative_base}'), {_delta}, {unit})"
    else:
        raise ValueError(f"Invalid scope: {scope}")


# ---------------------------------------------------------------------------
# pyparsing-based datetime expression evaluator
# ---------------------------------------------------------------------------


class EvalText:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[0]

    def eval(self) -> str:
        # strip quotes
        return self.value[1:-1]


class EvalDateTimeFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> datetime:
        return parse_human_datetime(self.value.eval())


class EvalDateAddFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> datetime:
        dttm_expression, delta, unit = self.value
        dttm = dttm_expression.eval()
        delta = delta.eval() if hasattr(delta, "eval") else delta
        if unit.lower() == "quarter":
            delta = delta * 3
            unit = "month"
        return dttm + parse_human_timedelta(f"{delta} {unit}s", dttm)


class EvalDateDiffFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> int:
        if len(self.value) == 2:
            _dttm_from, _dttm_to = self.value
            return (_dttm_to.eval() - _dttm_from.eval()).days

        if len(self.value) == 3:
            _dttm_from, _dttm_to, _unit = self.value
            if _unit == "year":
                return _dttm_to.eval().year - _dttm_from.eval().year
            if _unit == "day":
                return (_dttm_to.eval() - _dttm_from.eval()).days
        raise ValueError("Unable to calculate such a date delta")


class EvalDateTruncFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> datetime:
        dttm_expression, unit = self.value
        dttm = dttm_expression.eval()
        if unit == "year":
            dttm = dttm.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )
        if unit == "quarter":
            dttm = (
                pd.Period(pd.Timestamp(dttm), freq="Q").to_timestamp().to_pydatetime()
            )
        elif unit == "month":
            dttm = dttm.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif unit == "week":
            dttm -= relativedelta(days=dttm.weekday())
            dttm = dttm.replace(hour=0, minute=0, second=0, microsecond=0)
        elif unit == "day":
            dttm = dttm.replace(hour=0, minute=0, second=0, microsecond=0)
        elif unit == "hour":
            dttm = dttm.replace(minute=0, second=0, microsecond=0)
        elif unit == "minute":
            dttm = dttm.replace(second=0, microsecond=0)
        else:
            dttm = dttm.replace(microsecond=0)
        return dttm


class EvalLastDayFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> datetime:
        dttm_expression, unit = self.value
        dttm = dttm_expression.eval()
        if unit == "year":
            return dttm.replace(
                month=12, day=31, hour=0, minute=0, second=0, microsecond=0
            )
        if unit == "month":
            return dttm.replace(
                day=calendar.monthrange(dttm.year, dttm.month)[1],
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
        # unit == "week":
        mon = dttm - relativedelta(days=dttm.weekday())
        mon = mon.replace(hour=0, minute=0, second=0, microsecond=0)
        return mon + relativedelta(days=6)


class EvalHolidayFunc:
    def __init__(self, tokens: ParseResults) -> None:
        self.value = tokens[1]

    def eval(self) -> datetime:
        holiday = self.value[0].eval()
        dttm, country = [None, None]
        if len(self.value) >= 2:
            dttm = self.value[1].eval()
        if len(self.value) == 3:
            country = self.value[2]
        holiday_year = dttm.year if dttm else parse_human_datetime("today").year
        country = country.eval() if country else "US"

        holiday_lookup = country_holidays(country, years=[holiday_year], observed=False)
        searched_result = holiday_lookup.get_named(holiday, lookup="istartswith")
        if len(searched_result) > 0:
            return dttm_from_timetuple(searched_result[0].timetuple())
        raise ValueError(f"Unable to find such a holiday: [{holiday}]")


@lru_cache(maxsize=LRU_CACHE_MAX_SIZE)
def datetime_parser() -> ParseResults:
    (
        DATETIME,
        DATEADD,
        DATEDIFF,
        DATETRUNC,
        LASTDAY,
        HOLIDAY,
        YEAR,
        QUARTER,
        MONTH,
        WEEK,
        DAY,
        HOUR,
        MINUTE,
        SECOND,
    ) = map(
        CaselessKeyword,
        "datetime dateadd datediff datetrunc lastday holiday "
        "year quarter month week day hour minute second".split(),
    )
    lparen, rparen, comma = map(Suppress, "(),")
    text_operand = quotedString.setName("text_operand").setParseAction(EvalText)

    # allow expression to be used recursively
    datetime_func = Forward().setName("datetime")
    dateadd_func = Forward().setName("dateadd")
    datetrunc_func = Forward().setName("datetrunc")
    lastday_func = Forward().setName("lastday")
    holiday_func = Forward().setName("holiday")
    date_expr = (
        datetime_func | dateadd_func | datetrunc_func | lastday_func | holiday_func
    )

    # literal integer and expression that return a literal integer
    datediff_func = Forward().setName("datediff")
    int_operand = (
        pyparsing_common.signed_integer().setName("int_operand") | datediff_func
    )

    datetime_func <<= (DATETIME + lparen + text_operand + rparen).setParseAction(
        EvalDateTimeFunc
    )
    dateadd_func <<= (
        DATEADD
        + lparen
        + Group(
            date_expr
            + comma
            + int_operand
            + comma
            + (YEAR | QUARTER | MONTH | WEEK | DAY | HOUR | MINUTE | SECOND)
            + ppOptional(comma)
        )
        + rparen
    ).setParseAction(EvalDateAddFunc)
    datetrunc_func <<= (
        DATETRUNC
        + lparen
        + Group(
            date_expr
            + comma
            + (YEAR | QUARTER | MONTH | WEEK | DAY | HOUR | MINUTE | SECOND)
            + ppOptional(comma)
        )
        + rparen
    ).setParseAction(EvalDateTruncFunc)
    lastday_func <<= (
        LASTDAY
        + lparen
        + Group(date_expr + comma + (YEAR | MONTH | WEEK) + ppOptional(comma))
        + rparen
    ).setParseAction(EvalLastDayFunc)
    holiday_func <<= (
        HOLIDAY
        + lparen
        + Group(
            text_operand
            + ppOptional(comma)
            + ppOptional(date_expr)
            + ppOptional(comma)
            + ppOptional(text_operand)
            + ppOptional(comma)
        )
        + rparen
    ).setParseAction(EvalHolidayFunc)
    datediff_func <<= (
        DATEDIFF
        + lparen
        + Group(
            date_expr
            + comma
            + date_expr
            + ppOptional(comma + (YEAR | DAY) + ppOptional(comma))
        )
        + rparen
    ).setParseAction(EvalDateDiffFunc)

    return date_expr | datediff_func


def datetime_eval(datetime_expression: str | None = None) -> datetime | None:
    if datetime_expression:
        try:
            return datetime_parser().parseString(datetime_expression)[0].eval()
        except ParseException as ex:
            raise ValueError(ex) from ex
    return None


# ---------------------------------------------------------------------------
# get_since_until -- the main entry point
# ---------------------------------------------------------------------------


def get_since_until(  # noqa: C901
    time_range: str | None = None,
    since: str | None = None,
    until: str | None = None,
    time_shift: str | None = None,
    relative_start: str | None = None,
    relative_end: str | None = None,
    instant_time_comparison_range: str | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return ``since`` and ``until`` datetime tuple from string representations of
    time_range, since, until and time_shift.

    This function supports both reading the keys separately (from ``since`` and
    ``until``), as well as the new ``time_range`` key. Valid formats are:

        - ISO 8601
        - X days/years/hours/day/year/weeks
        - X days/years/hours/day/year/weeks ago
        - X days/years/hours/day/year/weeks from now
        - freeform

    Additionally, for ``time_range`` (these specify both ``since`` and ``until``):

        - Last day
        - Last week
        - Last month
        - Last quarter
        - Last year
        - No filter
        - Last X seconds/minutes/hours/days/weeks/months/years
        - Next X seconds/minutes/hours/days/weeks/months/years
    """
    separator = " : "
    _relative_start = relative_start if relative_start else "today"
    _relative_end = relative_end if relative_end else "today"

    if time_range == NO_TIME_RANGE:
        return None, None

    if time_range and time_range.startswith("Last") and separator not in time_range:
        time_range = time_range + separator + _relative_end

    if time_range and time_range.startswith("Next") and separator not in time_range:
        time_range = _relative_start + separator + time_range

    if (
        time_range
        and time_range.startswith("previous calendar week")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), -1, WEEK), WEEK) : "
            "DATETRUNC(DATETIME('today'), WEEK)"
        )
    if (
        time_range
        and time_range.startswith("previous calendar month")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), -1, MONTH), MONTH) : "
            "DATETRUNC(DATETIME('today'), MONTH)"
        )
    if (
        time_range
        and time_range.startswith("previous calendar quarter")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), -1, QUARTER), QUARTER) : "
            "DATETRUNC(DATETIME('today'), QUARTER)"
        )
    if (
        time_range
        and time_range.startswith("previous calendar year")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), -1, YEAR), YEAR) : "
            "DATETRUNC(DATETIME('today'), YEAR)"
        )
    if (
        time_range
        and time_range.startswith("Current day")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), 0, DAY), DAY) : "
            "DATETRUNC(DATEADD(DATETIME('today'), 1, DAY), DAY)"
        )
    if (
        time_range
        and time_range.startswith("Current week")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), 0, WEEK), WEEK) : "
            "DATETRUNC(DATEADD(DATETIME('today'), 1, WEEK), WEEK)"
        )
    if (
        time_range
        and time_range.startswith("Current month")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), 0, MONTH), MONTH) : "
            "DATETRUNC(DATEADD(DATETIME('today'), 1, MONTH), MONTH)"
        )
    if (
        time_range
        and time_range.startswith("Current quarter")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), 0, QUARTER), QUARTER) : "
            "DATETRUNC(DATEADD(DATETIME('today'), 1, QUARTER), QUARTER)"
        )
    if (
        time_range
        and time_range.startswith("Current year")
        and separator not in time_range
    ):
        time_range = (
            "DATETRUNC(DATEADD(DATETIME('today'), 0, YEAR), YEAR) : "
            "DATETRUNC(DATEADD(DATETIME('today'), 1, YEAR), YEAR)"
        )

    if time_range and separator in time_range:
        time_range_lookup = [
            (
                r"^(start of|beginning of|end of)\s+"
                r"(this|last|next|prior)\s+"
                r"([0-9]+)?\s*"
                r"(day|week|month|quarter|year)s?$",
                lambda modifier, scope, delta, unit: handle_modifier_and_unit(
                    modifier,
                    scope,
                    delta,
                    unit,
                    get_relative_base(unit, relative_start),
                ),
            ),
            (
                r"^(this|last|next|prior)\s+"
                r"([0-9]+)?\s*"
                r"(second|minute|day|week|month|quarter|year)s?$",
                lambda scope, delta, unit: handle_scope_and_unit(
                    scope, delta, unit, get_relative_base(unit, relative_start)
                ),
            ),
            (
                r"^(DATETIME.*|DATEADD.*|DATETRUNC.*|LASTDAY.*|HOLIDAY.*)$",
                lambda text: text,
            ),
        ]

        since_and_until_partition = [
            part.strip() for part in time_range.split(separator, 1)
        ]
        since_and_until: list[str | None] = []
        for part in since_and_until_partition:
            if not part:
                since_and_until.append(None)
                continue

            matched = False
            for pattern, fn in time_range_lookup:
                result = re.search(pattern, part, re.IGNORECASE)
                if result:
                    matched = True
                    since_and_until.append(fn(*result.groups()))  # type: ignore
            if not matched:
                since_and_until.append(f"DATETIME('{part}')")

        _since, _until = map(datetime_eval, since_and_until)
    else:
        since = since or ""
        if since:
            since = add_ago_to_since(since)
        _since = parse_human_datetime(since) if since else None
        _until = (
            parse_human_datetime(until)
            if until
            else parse_human_datetime(_relative_end)
        )

    if time_shift:
        separator = " : "
        if separator in time_shift:
            parts = time_shift.split(separator, 1)
            if len(parts) != 2:
                raise ValueError(f"Invalid time_shift format: {time_shift}")
            since_part, until_part = (part.strip() for part in parts)
            _since = parse_human_datetime(since_part)
            _until = parse_human_datetime(until_part)
        else:
            time_delta_since = parse_past_timedelta(time_shift, _since)
            time_delta_until = parse_past_timedelta(time_shift, _until)
            _since = _since if _since is None else (_since - time_delta_since)
            _until = _until if _until is None else (_until - time_delta_until)

    if instant_time_comparison_range:
        time_unit = ""
        delta_in_days = None
        if instant_time_comparison_range == InstantTimeComparison.YEAR:
            time_unit = "YEAR"
        elif instant_time_comparison_range == InstantTimeComparison.MONTH:
            time_unit = "MONTH"
        elif instant_time_comparison_range == InstantTimeComparison.WEEK:
            time_unit = "WEEK"
        elif instant_time_comparison_range == InstantTimeComparison.INHERITED:
            delta_in_days = (_until - _since).days if _since and _until else None
            time_unit = "DAY"

        if time_unit:
            strtfime_since = (
                _since.strftime("%Y-%m-%dT%H:%M:%S") if _since else relative_start
            )
            strtfime_until = (
                _until.strftime("%Y-%m-%dT%H:%M:%S") if _until else relative_end
            )

            since_and_until_exprs = [
                (
                    f"DATEADD(DATETIME('{strtfime_since}'), "
                    f"-{delta_in_days or 1}, {time_unit})"
                ),
                (
                    f"DATEADD(DATETIME('{strtfime_until}'), "
                    f"-{delta_in_days or 1}, {time_unit})"
                ),
            ]

            _since, _until = map(datetime_eval, since_and_until_exprs)

    if _since and _until and _since > _until:
        raise ValueError("From date cannot be larger than to date")

    return _since, _until


# ---------------------------------------------------------------------------
# normalize_dttm_col (from superset/utils/core.py)
# ---------------------------------------------------------------------------


def normalize_dttm_col(
    df: pd.DataFrame,
    dttm_cols: tuple[DateColumn, ...] = (),
) -> None:
    for _col in dttm_cols:
        if _col.col_label not in df.columns:
            continue

        if _col.timestamp_format in ("epoch_s", "epoch_ms"):
            dttm_series = df[_col.col_label]
            if is_numeric_dtype(dttm_series):
                unit = _col.timestamp_format.replace("epoch_", "")
                df[_col.col_label] = pd.to_datetime(
                    dttm_series,
                    utc=False,
                    unit=unit,
                    origin="unix",
                    errors="coerce",
                    exact=False,
                )
            else:
                try:
                    df[_col.col_label] = dttm_series.apply(
                        lambda x: pd.Timestamp(x) if pd.notna(x) else pd.NaT
                    )
                except ValueError:
                    logger.warning(
                        "Unable to convert column %s to datetime, ignoring",
                        _col.col_label,
                    )
        else:
            df[_col.col_label] = pd.to_datetime(
                df[_col.col_label],
                utc=False,
                format=_col.timestamp_format,
                errors="coerce",
                exact=False,
            )
        if _col.offset:
            df[_col.col_label] += timedelta(hours=_col.offset)
        if _col.time_shift is not None:
            df[_col.col_label] += parse_human_timedelta(_col.time_shift)
