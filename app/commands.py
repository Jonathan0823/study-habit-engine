COMMANDS = (
    ("help", "", "Show the eight supported commands and their usage"),
    ("today", "", "View today's study queue; create it only when absent"),
    (
        "done",
        "[block_id] | <token> <actual_minutes>",
        "Select a pending block by ID, then confirm with the displayed token and actual minutes",
    ),
    (
        "skip",
        "[block_id]",
        "Skip a pending block; choose from pending blocks when omitted",
    ),
    (
        "complete_topic",
        "[topic_id]",
        "Choose from listed topics when omitted; preview prerequisites and queue impact, then confirm completion",
    ),
    ("budget", "[minutes]", "Set today's 0-90 minute budget; omit to be prompted"),
    ("replan", "", "Request a replacement for today's unfinished queue"),
    (
        "progress",
        "[YYYY-MM-DD | add TOPIC MINUTES [YYYY-MM-DD] | edit SESSION_ID YYYY-MM-DD MINUTES | reopen TOPIC_ID | confirm TOKEN]",
        "Review history, record or correct sessions, or reopen a topic with confirmation",
    ),
)


def help_text() -> str:
    return "\n".join(
        f"/{name}{(' ' + arguments) if arguments else ''} - {description}"
        for name, arguments, description in COMMANDS
    )
