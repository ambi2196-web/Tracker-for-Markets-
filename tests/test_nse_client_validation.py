import unittest

from collect.nse_client import FetchResult, NSEClient, FetchError


def _result(content: bytes, content_type: str = "text/csv") -> FetchResult:
    return FetchResult(url="http://test", status_code=200, content=content, content_type=content_type, elapsed_seconds=0.1)


class TestContentValidation(unittest.TestCase):
    def test_rejects_html_error_page_disguised_as_csv(self):
        # The exact jugaad-data failure mode from Phase 0: an HTML 404 page saved as .csv
        html = b"<!DOCTYPE html><html><body>404 not found</body></html>"
        with self.assertRaises(FetchError):
            NSEClient._validate_content(_result(html), expect_zip=False)

    def test_rejects_html_regardless_of_case_or_leading_whitespace(self):
        html = b"   \n<HTML><body>error</body></html>"
        with self.assertRaises(FetchError):
            NSEClient._validate_content(_result(html), expect_zip=False)

    def test_accepts_plausible_csv(self):
        csv = b"SYMBOL, SERIES, DATE1\nRELIANCE, EQ, 03-Aug-2026\n" * 5
        NSEClient._validate_content(_result(csv), expect_zip=False)  # must not raise

    def test_rejects_suspiciously_small_response(self):
        with self.assertRaises(FetchError):
            NSEClient._validate_content(_result(b"x"), expect_zip=False)

    def test_zip_mode_requires_pk_magic_bytes(self):
        with self.assertRaises(FetchError):
            NSEClient._validate_content(_result(b"not a zip file but long enough to pass size check"), expect_zip=True)

    def test_zip_mode_accepts_pk_header(self):
        content = b"PK" + b"\x00" * 50
        NSEClient._validate_content(_result(content), expect_zip=True)  # must not raise


if __name__ == "__main__":
    unittest.main()
