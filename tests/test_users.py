import pytest

from hotfix_booking.users import resolve_jira_user


IVAN = {
    "accountId": "712020:ba19a0d3-f62c-4b6a-8d58-3b09c372721a",
    "displayName": "Ivan Queiroz",
    "emailAddress": "iqueiroz@neovest.com",
    "active": True,
}
QUEUE_ACCOUNT = {
    "accountId": "qm:50fd12cc-...",
    "displayName": "iqueiroz@neovest.com",
    "emailAddress": "",
    "active": True,
}


class TestResolveJiraUser:
    def test_picks_real_user_over_queue_stub(self) -> None:
        result = resolve_jira_user("iqueiroz@neovest.com", [IVAN, QUEUE_ACCOUNT])
        assert result == IVAN

    def test_order_doesnt_matter(self) -> None:
        result = resolve_jira_user("iqueiroz@neovest.com", [QUEUE_ACCOUNT, IVAN])
        assert result == IVAN

    def test_ignores_inactive_matches(self) -> None:
        inactive = {**IVAN, "active": False}
        assert resolve_jira_user("iqueiroz@neovest.com", [inactive]) is None

    def test_case_insensitive_email_match(self) -> None:
        result = resolve_jira_user("IQueiroz@NeoVest.com", [IVAN])
        assert result == IVAN

    def test_no_match_returns_none(self) -> None:
        assert resolve_jira_user("nobody@neovest.com", [IVAN, QUEUE_ACCOUNT]) is None

    def test_empty_email_returns_none(self) -> None:
        assert resolve_jira_user("", [IVAN]) is None

    def test_empty_user_list_returns_none(self) -> None:
        assert resolve_jira_user("iqueiroz@neovest.com", []) is None

    def test_stub_with_empty_email_never_matches(self) -> None:
        # Even if the query is the same string as the stub's displayName, we
        # must not fall back to the stub — it has no emailAddress.
        assert resolve_jira_user("iqueiroz@neovest.com", [QUEUE_ACCOUNT]) is None
