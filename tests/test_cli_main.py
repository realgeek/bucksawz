"""Tests for main()'s AWS-error-to-short-message translation (vs. a stack trace)."""
import botocore.exceptions
import pytest
from bucksawz import cli


def _run_main_with(monkeypatch, exc):
    def _raise():
        raise exc
    monkeypatch.setattr(cli, "cli", _raise)
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_main_reports_profile_not_found_without_traceback(monkeypatch, capsys):
    _run_main_with(monkeypatch, botocore.exceptions.ProfileNotFound(profile="nope"))
    err = capsys.readouterr().err
    assert "AWS authentication failed" in err
    assert "Traceback" not in err


def test_main_reports_expired_sso_token_without_traceback(monkeypatch, capsys):
    _run_main_with(monkeypatch, botocore.exceptions.TokenRetrievalError(provider="sso", error_msg="expired"))
    err = capsys.readouterr().err
    assert "AWS authentication failed" in err
    assert "aws-vault" in err


def test_main_reports_client_error_without_traceback(monkeypatch, capsys):
    exc = botocore.exceptions.ClientError(
        error_response={"Error": {"Code": "AccessDenied", "Message": "nope"}},
        operation_name="GetCostAndUsage",
    )
    _run_main_with(monkeypatch, exc)
    err = capsys.readouterr().err
    assert "AWS API call failed" in err
    assert "Traceback" not in err
