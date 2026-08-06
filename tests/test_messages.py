"""Tests for notifications and messages."""

from unittest.mock import Mock

from worsaga.messages import (
    _sender,
    get_messages,
    get_notifications,
    normalize_messages,
    normalize_notifications,
)


def test_normalize_notifications_sorts_and_cleans():
    result = normalize_notifications({
        "notifications": [
            {
                "id": 1,
                "subject": "Older",
                "fullmessagehtml": "<p>Old</p>",
                "timecreated": 10,
                "read": 1,
            },
            {
                "id": 2,
                "subject": "Newer",
                "fullmessagehtml": "<p>New</p>",
                "timecreated": 20,
                "read": 0,
                "userfromfullname": "Tutor",
            },
        ],
    })
    assert [row["id"] for row in result] == [2, 1]
    assert result[0]["body_preview"] == "New"
    assert result[0]["read"] is False
    assert result[0]["sender"] == "Tutor"


def test_normalize_messages_accepts_list_payload():
    result = normalize_messages([
        {
            "messageid": "m1",
            "smallmessage": "Hello",
            "fullmessage": "Body",
            "timecreated": 10,
            "fromfullname": "Tutor",
        },
    ])
    assert result[0]["id"] == "m1"
    assert result[0]["type"] == "message"
    assert result[0]["subject"] == "Hello"


def test_get_notifications_filters_unread_locally():
    client = Mock()
    client.get_popup_notifications.return_value = {
        "notifications": [
            {"id": 1, "subject": "Read", "read": 1},
            {"id": 2, "subject": "Unread", "read": 0},
        ],
    }
    result = get_notifications(client, unread_only=True)
    assert [row["subject"] for row in result] == ["Unread"]


def test_sender_prefers_full_name():
    assert _sender({"userfromfullname": "Dr Avery Demo"}) == "Dr Avery Demo"
    assert _sender({"fromfullname": "Prof Riley Sample"}) == "Prof Riley Sample"
    assert _sender({"userfrom": {"fullname": "Nested Name"}}) == "Nested Name"


def test_sender_falls_back_to_user_id_label_when_name_absent():
    # message_popup_get_popup_notifications on many instances returns only a
    # numeric useridfrom and no name field, which left the Sender column
    # blank (Issue 3). Fall back to a "User <id>" label rather than "".
    assert _sender({"useridfrom": 4242}) == "User 4242"
    assert _sender({"userfromid": 77}) == "User 77"


def test_sender_omitted_when_nothing_identifies_the_sender():
    assert _sender({}) == ""
    # A zero/negative id is not a real user and must not become "User 0".
    assert _sender({"useridfrom": 0}) == ""


def test_notification_sender_uses_userfromfullname_when_present():
    # A realistic popup payload where the sender's name IS populated.
    result = normalize_notifications({
        "notifications": [
            {"id": 1, "subject": "Assignment due", "useridfrom": 55,
             "userfromfullname": "Course Team", "timecreated": 30},
        ],
    })
    assert result[0]["sender"] == "Course Team"


def test_notification_sender_falls_back_to_user_id_label():
    # A realistic popup payload where ONLY the numeric useridfrom is present.
    result = normalize_notifications({
        "notifications": [
            {"id": 2, "subject": "New material", "useridfrom": 4242,
             "timecreated": 20},
        ],
    })
    assert result[0]["sender"] == "User 4242"


def test_get_messages_filters_since_locally():
    client = Mock()
    client.get_messages.return_value = {
        "messages": [
            {"id": 1, "smallmessage": "Old", "timecreated": 10},
            {"id": 2, "smallmessage": "New", "timecreated": 100},
        ],
    }
    result = get_messages(client, since_days=1)
    # The exact cutoff depends on current time, so a very old fixture is filtered.
    assert result == []
