"""Camera credentials must not survive into a log line or an audit payload.

Not every camera puts its password in the userinfo. The doorbell streams
HTTP-FLV and carries `user` and `password` as ordinary query parameters, and
that URL went through the masking untouched — into the logs, and into the
audit payload written precisely so that passwords would not land there.
"""

from baba_core.url import mask_credentials

FLV = ("http://192.168.10.12/flv?port=1935&app=bcs"
       "&stream=channel0_main.bcs&user=admin&password=hunter2")


def test_the_userinfo_form_is_removed():
    assert mask_credentials("rtsp://admin:secret@cam.lan:554/stream") == (
        "rtsp://cam.lan:554/stream")


def test_the_query_form_is_redacted():
    masked = mask_credentials(FLV)
    assert "hunter2" not in masked and "admin" not in masked
    # The parameter stays, so an operator can see the URL carries one.
    assert "password=***" in masked and "user=***" in masked
    # Everything that is not a credential is left alone.
    assert "stream=channel0_main.bcs" in masked and "port=1935" in masked


def test_both_forms_at_once():
    masked = mask_credentials("rtsp://admin:secret@cam.lan:554/x?token=abc")
    assert masked == "rtsp://cam.lan:554/x?token=***"


def test_a_url_with_nothing_to_hide_is_returned_as_it_came():
    plain = "rtsp://cam.lan:554/h264Preview_01_main"
    assert mask_credentials(plain) is plain


def test_nonsense_is_returned_rather_than_raised():
    """It runs inside log calls; a crash here takes the log line with it."""
    assert mask_credentials("not a url at all") == "not a url at all"
    assert mask_credentials("") == ""
