"""Tests for live digest aggregation."""

from unittest.mock import patch

from worsaga.digest import get_digest


class FakeDigestClient:
    pass


def test_digest_combines_sources_and_warnings():
    with patch("worsaga.digest.get_upcoming_deadlines", return_value=[{"name": "Due"}]), \
         patch("worsaga.digest.get_assignments", side_effect=RuntimeError("assignments denied")), \
         patch("worsaga.digest.get_latest_updates", return_value=[{"name": "Announcement"}]), \
         patch("worsaga.digest.get_notifications", return_value=[]), \
         patch("worsaga.digest.get_messages", return_value=[{"subject": "Hi"}]):
        digest = get_digest(FakeDigestClient(), since_days=1)

    assert digest["deadlines"] == [{"name": "Due"}]
    assert digest["assignments"] == []
    assert digest["updates"] == [{"name": "Announcement"}]
    assert digest["messages"] == [{"subject": "Hi"}]
    assert digest["warnings"] == ["assignments: assignments denied"]
