"""Google Calendar event formatting and the narrowly scoped REST boundary."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

JAKARTA = ZoneInfo("Asia/Jakarta")
CALENDAR_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class CalendarError(Exception):
    """Base class for Calendar API failures safe to record without credentials."""


class CalendarAuthRequired(CalendarError):
    """The user must reconnect or restore valid OAuth credentials."""


class CalendarNotFound(CalendarError):
    """An app-owned event was deleted outside the application."""


class CalendarConflict(CalendarError):
    """A stable event ID already exists after an ambiguous earlier insert."""


class CalendarTransientError(CalendarError):
    """A network or provider error that can be retried safely."""


def build_calendar_event(
    study_date: date,
    topics: list[dict],
    *,
    now: datetime | None = None,
) -> dict:
    """Build a private reminder containing only topic titles and planned minutes."""
    start = datetime.combine(study_date, time(17, 0), JAKARTA)
    if now is not None:
        if now.tzinfo is None:
            raise ValueError("calendar event time requires a timezone-aware datetime")
        local_now = now.astimezone(JAKARTA)
        if local_now >= start:
            # A newly recovered reminder must still be actionable; never backdate a
            # Calendar notification and never move an already-existing event here.
            start = (local_now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=5)
    description = "Review today's flexible study queue:\n" + "\n".join(
        f"{index}. {topic['title']} — {topic['planned_minutes']} min"
        for index, topic in enumerate(topics, 1)
    )
    return {
        "summary": "Study plan reminder",
        "description": description,
        "visibility": "private",
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Jakarta"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Jakarta"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 0}],
        },
        "extendedProperties": {
            "private": {"app": "study-habit-engine", "study_date": study_date.isoformat()}
        },
    }


def calendar_event_id(study_date: date) -> str:
    """Return a stable Calendar event ID (lowercase base32hex alphabet)."""
    return f"shengine{study_date:%Y%m%d}"


async def persist_calendar_reminder(conn, study_date: date, plan_id, content: dict, blocks) -> None:
    """Persist an event intent without resource links, history, or OAuth secrets."""
    if isinstance(content, str):
        content = json.loads(content)
    topic_titles = {topic["topic_id"]: topic["title"] for topic in content["topics"]}
    topics = [
        {"title": topic_titles[block["topic_id"]], "planned_minutes": block["planned_minutes"]}
        for block in blocks
    ]
    if not topics:
        return
    serialized = json.dumps(topics, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(serialized.encode()).hexdigest()
    await conn.execute(
        """INSERT INTO calendar_reminders(
               study_date,plan_id,event_id,topics,payload_hash,state
           ) VALUES($1,$2,$3,$4::jsonb,$5,'pending')
           ON CONFLICT(study_date) DO UPDATE SET
               plan_id=EXCLUDED.plan_id,event_id=EXCLUDED.event_id,
               topics=EXCLUDED.topics,payload_hash=EXCLUDED.payload_hash,
               state=CASE WHEN calendar_reminders.state IN ('suppressed','auth_required')
                          THEN calendar_reminders.state ELSE 'pending' END,
               ever_synced=CASE WHEN calendar_reminders.state='cancelled'
                                THEN false ELSE calendar_reminders.ever_synced END,
               first_attempt_at=NULL,retry_until=NULL,next_retry_at=NULL,
               next_check_at=NULL,attempts=0,last_error_code=NULL,updated_at=now()""",
        study_date,
        plan_id,
        calendar_event_id(study_date),
        serialized,
        payload_hash,
    )


class GoogleCalendarClient:
    """Minimal primary-calendar REST client; the refresh token stays in a host file."""

    def __init__(self, credentials_file: str | Path, client: httpx.AsyncClient | None = None):
        self.credentials_file = Path(credentials_file)
        self.http = client or httpx.AsyncClient(timeout=15)
        self._owns_client = client is None
        self._credentials_file_mtime: float | None = None
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    def _credentials(self) -> dict:
        try:
            mode = stat.S_IMODE(self.credentials_file.stat().st_mode)
            if mode & 0o077:
                raise CalendarAuthRequired("Calendar credential file must be mode 0600")
            credentials = json.loads(self.credentials_file.read_text())
        except CalendarError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise CalendarAuthRequired("Calendar credential file is unavailable or invalid") from exc
        required = {"client_id", "client_secret", "refresh_token"}
        if not required <= credentials.keys():
            raise CalendarAuthRequired("Calendar credential file is missing required fields")
        return credentials

    async def _token(self) -> str:
        import time as clock

        current_mtime = _credential_file_mtime(self)
        if current_mtime != self._credentials_file_mtime:
            self._access_token = None
            self._credentials_file_mtime = current_mtime
        if self._access_token and clock.time() < self._access_token_expires_at - 30:
            return self._access_token
        credentials = self._credentials()
        try:
            response = await self.http.post(
                TOKEN_URL,
                data={
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "refresh_token": credentials["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise CalendarTransientError("Calendar token refresh failed") from exc
        if response.status_code in (400, 401, 403):
            raise CalendarAuthRequired("Calendar authorization must be renewed")
        if response.status_code >= 500 or response.status_code == 429:
            raise CalendarTransientError("Calendar token refresh is temporarily unavailable")
        if response.is_error:
            raise CalendarAuthRequired("Calendar authorization must be renewed")
        data = response.json()
        self._access_token = data["access_token"]
        self._access_token_expires_at = clock.time() + int(data.get("expires_in", 3600))
        return self._access_token

    async def request(self, method: str, event_id: str | None = None, *, body: dict | None = None):
        token = await self._token()
        url = CALENDAR_API if event_id is None else f"{CALENDAR_API}/{event_id}"
        try:
            response = await self.http.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise CalendarTransientError("Calendar API request failed") from exc
        if response.status_code in (404, 410):
            raise CalendarNotFound("The app-owned Calendar event no longer exists")
        if response.status_code == 409:
            raise CalendarConflict("The stable Calendar event ID already exists")
        if response.status_code == 401:
            raise CalendarAuthRequired("Calendar authorization must be renewed")
        if response.status_code == 403:
            try:
                errors = response.json().get("error", {}).get("errors", [])
                reasons = {error.get("reason") for error in errors}
            except (ValueError, AttributeError):
                reasons = set()
            if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}:
                raise CalendarTransientError("Calendar API quota is temporarily unavailable")
            raise CalendarAuthRequired("Calendar authorization must be renewed")
        if response.status_code == 429 or response.status_code >= 500:
            raise CalendarTransientError("Calendar API is temporarily unavailable")
        if response.is_error:
            raise CalendarError(f"Calendar API returned HTTP {response.status_code}")
        return response.json() if response.content else None

    async def close(self) -> None:
        if self._owns_client:
            await self.http.aclose()


async def sync_calendar_reminders(pool, client, now: datetime | None = None) -> None:
    """Idempotently sync today's app-owned reminder; never enumerate the calendar."""
    local_now = (now or datetime.now(JAKARTA)).astimezone(JAKARTA)
    if now is not None and now.tzinfo is None:
        raise ValueError("calendar sync requires a timezone-aware datetime")
    study_date = local_now.date()
    due_at = datetime.combine(study_date, time(17, 0), JAKARTA)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM calendar_reminders WHERE study_date=$1 AND (
                   (state='pending' AND (next_retry_at IS NULL OR next_retry_at <= $2)) OR
                   (state='synced' AND next_check_at IS NOT NULL) OR
                   (state='cancelled' AND ever_synced AND next_retry_at IS NOT NULL AND next_retry_at <= $2) OR
                   state='auth_required') ORDER BY study_date""",
            study_date,
            local_now,
        )
    for row in rows:
        if row["state"] == "auth_required":
            file_mtime = _credential_file_mtime(client)
            old_mtime = row["auth_file_mtime"]
            if file_mtime is None or (old_mtime is not None and file_mtime <= old_mtime):
                continue
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE calendar_reminders SET state='pending',auth_file_mtime=NULL,
                           first_attempt_at=NULL,retry_until=NULL,next_retry_at=NULL,
                           attempts=0,last_error_code=NULL,updated_at=now()
                       WHERE study_date=$1 AND state='auth_required'""",
                    study_date,
                )
            row = dict(row)
            row["state"] = "pending"
            row["ever_synced"] = False if not row["ever_synced"] else row["ever_synced"]
            row["next_retry_at"] = None

        async with pool.acquire() as conn:
            day = await conn.fetchrow(
                """SELECT d.budget_minutes,d.current_plan_id,d.state,
                          count(b.block_id) FILTER (WHERE b.status='pending')::int AS pending_count
                   FROM study_days d LEFT JOIN study_blocks b ON b.plan_id=d.current_plan_id
                   WHERE d.study_date=$1 GROUP BY d.study_date""",
                study_date,
            )
        has_current_queue = bool(
            day
            and day["state"] == "valid"
            and day["current_plan_id"] == row["plan_id"]
            and day["budget_minutes"] > 0
            and day["pending_count"] > 0
        )
        if not has_current_queue:
            if local_now < due_at:
                await _cancel_calendar_reminder(pool, client, row, local_now)
            elif not row["ever_synced"]:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE calendar_reminders SET state='cancelled',next_check_at=NULL,updated_at=now() WHERE study_date=$1",
                        study_date,
                    )
            else:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE calendar_reminders SET state='synced',next_check_at=NULL,updated_at=now() WHERE study_date=$1",
                        study_date,
                    )
            continue
        if (
            row["state"] == "synced"
            and row["next_check_at"] is not None
            and local_now < row["next_check_at"]
        ):
            continue

        await _begin_calendar_attempt(pool, study_date, local_now)
        topics = row["topics"]
        if isinstance(topics, str):
            topics = json.loads(topics)
        event = build_calendar_event(
            study_date, topics, now=local_now if not row["ever_synced"] else None
        )
        event_id = row["event_id"]
        try:
            try:
                existing = await client.request("GET", event_id)
            except CalendarNotFound:
                existing = None
            if existing is not None:
                if existing.get("status") == "cancelled":
                    if row["ever_synced"]:
                        await _set_calendar_state(
                            pool,
                            study_date,
                            "suppressed",
                            "EVENT_DELETED",
                            ever_synced=True,
                        )
                        continue
                    if not _is_our_event(existing, study_date):
                        raise CalendarError(
                            "The cancelled event is not confirmed as app-owned"
                        )
                    # Events.update is the full-resource operation that restores a
                    # cancelled organizer event when its status is set to confirmed.
                    restored = await client.request(
                        "PUT",
                        event_id,
                        body={**event, "id": event_id, "status": "confirmed"},
                    )
                    if (
                        restored is None
                        or restored.get("status") != "confirmed"
                        or not _is_our_event(restored, study_date)
                    ):
                        raise CalendarError("Calendar did not confirm event restoration")
                else:
                    if not _is_our_event(existing, study_date):
                        raise CalendarError(
                            "The stable event ID is not owned by this application"
                        )
                    if row["payload_hash"] != row["synced_payload_hash"]:
                        await client.request("PATCH", event_id, body=event)
            else:
                if row["ever_synced"]:
                    await _set_calendar_state(
                        pool, study_date, "suppressed", "EVENT_DELETED", ever_synced=True
                    )
                    continue
                create_body = {**event, "id": event_id}
                try:
                    created = await client.request("POST", body=create_body)
                except CalendarConflict:
                    created = await client.request("GET", event_id)
                    if not _is_our_event(created, study_date):
                        raise CalendarError("The stable event ID is not owned by this application")
                    await client.request("PATCH", event_id, body=event)
                if created is not None and not _is_our_event(created, study_date):
                    raise CalendarError("Calendar did not confirm the app-owned event")
            await _set_calendar_success(
                pool,
                study_date,
                row["payload_hash"],
                next_check_at=due_at if local_now < due_at else None,
            )
        except CalendarAuthRequired:
            await _set_calendar_state(
                pool,
                study_date,
                "auth_required",
                "AUTH_REQUIRED",
                auth_file_mtime=_credential_file_mtime(client),
            )
        except CalendarTransientError:
            await _set_calendar_transient_failure(pool, study_date, local_now)
        except CalendarError:
            await _set_calendar_state(pool, study_date, "failed", "API_REJECTED")


async def _begin_calendar_attempt(pool, study_date: date, now: datetime) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE calendar_reminders SET
                   first_attempt_at=COALESCE(first_attempt_at,$2),
                   retry_until=COALESCE(retry_until,$2 + interval '1 hour'),
                   attempts=attempts+1,updated_at=now()
               WHERE study_date=$1""",
            study_date,
            now,
        )


async def _set_calendar_success(pool, study_date: date, payload_hash: str, *, next_check_at):
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE calendar_reminders SET state='synced',ever_synced=true,
                   synced_payload_hash=$2,next_check_at=$3,next_retry_at=NULL,
                   first_attempt_at=NULL,retry_until=NULL,attempts=0,
                   last_error_code=NULL,auth_file_mtime=NULL,updated_at=now()
               WHERE study_date=$1""",
            study_date,
            payload_hash,
            next_check_at,
        )


async def _set_calendar_state(
    pool,
    study_date: date,
    state: str,
    error_code: str | None,
    *,
    ever_synced: bool | None = None,
    auth_file_mtime: float | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE calendar_reminders SET state=$2,last_error_code=$3,
                   ever_synced=COALESCE($4,ever_synced),auth_file_mtime=$5,
                   next_retry_at=NULL,next_check_at=NULL,updated_at=now()
               WHERE study_date=$1""",
            study_date,
            state,
            error_code,
            ever_synced,
            auth_file_mtime,
        )


async def _set_calendar_transient_failure(pool, study_date: date, now: datetime) -> None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT retry_until,attempts,state FROM calendar_reminders WHERE study_date=$1",
            study_date,
        )
        if row is None:
            return
        if row["retry_until"] is not None and now >= row["retry_until"]:
            await conn.execute(
                "UPDATE calendar_reminders SET state='failed',last_error_code='RETRY_EXHAUSTED',next_retry_at=NULL,next_check_at=NULL,updated_at=now() WHERE study_date=$1",
                study_date,
            )
            return
        delay_seconds = min(30 * (2 ** min(row["attempts"], 5)), 900)
        retry_state = "cancelled" if row["state"] == "cancelled" else "pending"
        await conn.execute(
            """UPDATE calendar_reminders SET state=$2,last_error_code='TRANSIENT',
                   next_retry_at=$3::timestamptz + make_interval(secs => $4::int),
                   updated_at=now()
               WHERE study_date=$1""",
            study_date,
            retry_state,
            now,
            delay_seconds,
        )


async def _cancel_calendar_reminder(pool, client, row, now: datetime) -> None:
    study_date = row["study_date"]
    if not row["ever_synced"]:
        await _set_calendar_state(pool, study_date, "cancelled", None, ever_synced=False)
        return
    await _begin_calendar_attempt(pool, study_date, now)
    try:
        await client.request("DELETE", row["event_id"])
        await _set_calendar_state(pool, study_date, "cancelled", None, ever_synced=False)
    except CalendarNotFound:
        await _set_calendar_state(pool, study_date, "cancelled", None, ever_synced=False)
    except CalendarAuthRequired:
        await _set_calendar_state(
            pool,
            study_date,
            "auth_required",
            "AUTH_REQUIRED",
            auth_file_mtime=_credential_file_mtime(client),
        )
    except CalendarTransientError:
        await _set_calendar_transient_failure(pool, study_date, now)
    except CalendarError:
        await _set_calendar_state(pool, study_date, "failed", "API_REJECTED")


def _is_our_event(event: dict, study_date: date) -> bool:
    properties = (event.get("extendedProperties") or {}).get("private") or {}
    return (
        properties.get("app") == "study-habit-engine"
        and properties.get("study_date") == study_date.isoformat()
    )


def _credential_file_mtime(client) -> float | None:
    credentials_file = getattr(client, "credentials_file", None)
    if credentials_file is None:
        return None
    try:
        info = Path(credentials_file).stat()
        return max(info.st_mtime, info.st_ctime)
    except OSError:
        return None
