import asyncio
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.calendar import (
    CalendarAuthRequired,
    CalendarNotFound,
    GoogleCalendarClient,
    build_calendar_event,
)


def test_calendar_event_is_private_and_contains_only_topic_titles_and_minutes():
    event = build_calendar_event(
        date(2026, 11, 2),
        [
            {
                "title": "Linear algebra",
                "planned_minutes": 25,
                "resources": [{"title": "Course notes", "url": "https://example.test/notes"}],
            },
            {"title": "Probability", "planned_minutes": 20, "resources": []},
        ],
    )

    assert event["visibility"] == "private"
    assert event["summary"] == "Study plan reminder"
    assert event["start"] == {
        "dateTime": "2026-11-02T17:00:00+07:00",
        "timeZone": "Asia/Jakarta",
    }
    assert event["end"] == {
        "dateTime": "2026-11-02T17:05:00+07:00",
        "timeZone": "Asia/Jakarta",
    }
    assert event["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 0}],
    }
    assert event["description"] == (
        "Review today's flexible study queue:\n"
        "1. Linear algebra — 25 min\n"
        "2. Probability — 20 min"
    )
    assert "example.test" not in str(event)
    assert event["extendedProperties"]["private"]["app"] == "study-habit-engine"


def test_late_calendar_recovery_uses_an_immediate_event_time():
    event = build_calendar_event(
        date(2026, 11, 2),
        [{"title": "Linear algebra", "planned_minutes": 25}],
        now=datetime(2026, 11, 2, 20, 0, tzinfo=ZoneInfo("Asia/Jakarta")),
    )

    assert event["start"]["dateTime"] == "2026-11-02T20:01:00+07:00"


def test_google_calendar_client_uses_primary_events_and_private_file_credentials(tmp_path):
    credentials_file = tmp_path / "calendar.json"
    credentials_file.write_text(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-secret",
            }
        )
    )
    credentials_file.chmod(0o600)
    observed = []

    def handler(request):
        observed.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600})
        assert request.url.path == "/calendar/v3/calendars/primary/events"
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.method == "POST"
        return httpx.Response(200, json={"id": "shengine20261102"})

    async def send_event():
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://www.googleapis.com",
        )
        client = GoogleCalendarClient(credentials_file, http)
        try:
            return await client.request("POST", body={"id": "shengine20261102"})
        finally:
            await http.aclose()

    result = asyncio.run(send_event())
    assert result["id"] == "shengine20261102"
    assert len(observed) == 2
    assert "refresh-secret" not in str(result)


@pytest.mark.parametrize("status_code", [404, 410])
def test_google_calendar_client_treats_missing_or_gone_events_as_not_found(
    tmp_path, status_code
):
    credentials_file = tmp_path / "calendar.json"
    credentials_file.write_text(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-secret",
            }
        )
    )
    credentials_file.chmod(0o600)

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "access-token"})
        return httpx.Response(status_code)

    async def fetch_event():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = GoogleCalendarClient(credentials_file, http)
        try:
            with pytest.raises(CalendarNotFound):
                await client.request("GET", "shengine20261102")
        finally:
            await http.aclose()

    asyncio.run(fetch_event())


def test_google_calendar_client_rejects_group_or_world_readable_credentials(tmp_path):
    credentials_file = tmp_path / "calendar.json"
    credentials_file.write_text(
        json.dumps(
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "refresh_token": "refresh-secret",
            }
        )
    )
    credentials_file.chmod(0o644)

    async def refresh():
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: None))
        client = GoogleCalendarClient(credentials_file, http)
        try:
            with pytest.raises(CalendarAuthRequired):
                await client.request("GET", "shengine20261102")
        finally:
            await http.aclose()

    asyncio.run(refresh())
