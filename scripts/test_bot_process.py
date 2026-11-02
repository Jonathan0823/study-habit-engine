import asyncio
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import asyncpg

UPDATE_ID = 910001


class TelegramMock(BaseHTTPRequestHandler):
    offsets: ClassVar[list[int]] = []
    sent_messages: ClassVar[list[dict]] = []
    ack_received: ClassVar[threading.Event] = threading.Event()
    release_ack: ClassVar[threading.Event] = threading.Event()

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        if self.path == "/bottest-token/getUpdates":
            offset = body["offset"]
            self.offsets.append(offset)
            result = []
            if offset > UPDATE_ID:
                self.ack_received.set()
                self.release_ack.wait(timeout=10)
            if offset <= UPDATE_ID:
                update = {
                    "update_id": UPDATE_ID,
                    "message": {
                        "from": {"id": 111},
                        "chat": {"id": 111, "type": "private"},
                        "text": "/help",
                    },
                }
                result = [update, update]
            self.respond({"ok": True, "result": result})
        elif self.path == "/bottest-token/sendMessage":
            self.sent_messages.append(body)
            self.respond({"ok": True, "result": {"message_id": 42}})
        else:
            self.send_error(404)

    def respond(self, content):
        encoded = json.dumps(content).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass


async def persisted_path():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        operation = await conn.fetchrow("SELECT status FROM operations")
        ingestion = await conn.fetchrow("SELECT update_id FROM telegram_ingestion")
        cursor = await conn.fetchval(
            "SELECT next_offset FROM telegram_cursor WHERE bot_id=12345"
        )
        delivery = await conn.fetchrow(
            "SELECT status, telegram_receipt FROM deliveries"
        )
        counts = await conn.fetchrow(
            "SELECT (SELECT count(*) FROM operations) AS operations, (SELECT count(*) FROM deliveries) AS deliveries"
        )
        return operation, ingestion, cursor, delivery, counts
    finally:
        await conn.close()


def main():
    async def reset_database():
        conn = await asyncpg.connect(os.environ["MIGRATION_DATABASE_URL"])
        try:
            await conn.execute(
                "TRUNCATE deliveries, telegram_ingestion, operations, telegram_cursor CASCADE"
            )
        finally:
            await conn.close()

    asyncio.run(reset_database())
    subprocess.run(
        ["cargo", "build", "--quiet", "--bin", "study-habit-engine-bot"], check=True
    )
    mock = ThreadingHTTPServer(("127.0.0.1", 0), TelegramMock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    env = os.environ | {
        "API_URL": os.environ["TEST_API_URL"],
        "TELEGRAM_API_URL": f"http://127.0.0.1:{mock.server_port}",
    }
    processes = []

    def launch():
        process = subprocess.Popen(["target/debug/study-habit-engine-bot"], env=env)
        processes.append(process)
        return process

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    try:
        bot = launch()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            operation, ingestion, cursor, delivery, counts = asyncio.run(
                persisted_path()
            )
            if delivery and delivery["status"] == "sent":
                break
            if bot.poll() is not None:
                raise AssertionError(
                    f"bot exited unexpectedly with status {bot.returncode}"
                )
            time.sleep(0.1)
        else:
            raise AssertionError("bot did not persist and send the /help delivery")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not TelegramMock.ack_received.is_set():
            time.sleep(0.05)
        assert TelegramMock.ack_received.is_set(), (
            "polling did not acknowledge the durable next offset"
        )
        stop(bot)
        TelegramMock.release_ack.set()

        assert operation["status"] == "accepted"
        assert ingestion["update_id"] == UPDATE_ID
        assert cursor == UPDATE_ID + 1
        assert delivery["telegram_receipt"] == "42"
        assert dict(counts) == {"operations": 1, "deliveries": 1}
        assert TelegramMock.offsets[0] == 0
        assert len(TelegramMock.sent_messages) == 1
        sent = TelegramMock.sent_messages[0]
        assert sent["chat_id"] == "111"
        assert sent["text"].startswith("/help - Show supported commands")
        print(
            "Rust bot process: redelivery stayed idempotent, durable offset acknowledged, /help sent"
        )
    finally:
        TelegramMock.release_ack.set()
        for process in processes:
            stop(process)
        mock.shutdown()
        mock.server_close()


if __name__ == "__main__":
    main()
