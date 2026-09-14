"""The pure static-path resolver: URL path in, candidate file out - no filesystem involved.

Every case here runs with a static root that does not exist, which is the point: the resolver is a
pure function, so traversal refusal is provable without creating a single file (Stage 4 U13).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from api.http_server import (
    STATIC_CONTENT_TYPES,
    STATIC_EXTENSIONS,
    STATIC_ROOT,
    StaticPathError,
    resolve_static_path,
    static_request_for,
)

ROOT = Path("web")
NONEXISTENT_ROOT = Path("definitely") / "not" / "a" / "directory"


class AcceptedPathTests(unittest.TestCase):
    def test_a_simple_asset_resolves_under_the_root(self) -> None:
        request = resolve_static_path("/app.js", ROOT)
        self.assertEqual(request.relative_path, "app.js")
        self.assertEqual(request.candidate, ROOT / "app.js")
        self.assertEqual(request.content_type, "application/javascript")
        self.assertEqual(request.url_path, "/app.js")

    def test_nested_assets_and_every_allowed_extension(self) -> None:
        expected = {
            "/index.html": "text/html",
            "/page.htm": "text/html",
            "/styles/app.css": "text/css",
            "/js/app.js": "application/javascript",
            "/js/module.mjs": "application/javascript",
        }
        for url_path, content_type in expected.items():
            with self.subTest(url_path=url_path):
                request = resolve_static_path(url_path, ROOT)
                self.assertEqual(request.content_type, content_type)
                self.assertTrue(str(request.candidate).replace("\\", "/").endswith(url_path.lstrip("/")))

    def test_the_allow_list_is_the_documented_one(self) -> None:
        self.assertEqual(set(STATIC_CONTENT_TYPES), set(STATIC_EXTENSIONS))
        self.assertEqual(
            STATIC_EXTENSIONS, (".css", ".htm", ".html", ".js", ".mjs")
        )

    def test_a_query_string_is_ignored_and_does_not_change_the_candidate(self) -> None:
        request = resolve_static_path("/app.js?v=2#frag", ROOT)
        self.assertEqual(request.relative_path, "app.js")
        self.assertEqual(request.candidate, ROOT / "app.js")

    def test_extension_matching_is_case_insensitive(self) -> None:
        self.assertEqual(resolve_static_path("/APP.JS", ROOT).content_type, "application/javascript")

    def test_the_resolver_never_touches_the_filesystem(self) -> None:
        """A root that does not exist resolves exactly like one that does."""
        existing = resolve_static_path("/app.js", ROOT)
        missing = resolve_static_path("/app.js", NONEXISTENT_ROOT)
        self.assertEqual(existing.relative_path, missing.relative_path)
        self.assertEqual(existing.content_type, missing.content_type)
        self.assertEqual(
            missing.candidate, NONEXISTENT_ROOT / "app.js"
        )

    def test_static_request_for_is_the_documented_alias(self) -> None:
        self.assertEqual(
            static_request_for("/app.js", ROOT).candidate,
            resolve_static_path("/app.js", ROOT).candidate,
        )

    def test_the_default_root_is_web_under_the_repository(self) -> None:
        self.assertEqual(STATIC_ROOT.name, "web")


class TraversalRefusalTests(unittest.TestCase):
    """``..`` in any form is refused before any file could be opened."""

    def assert_refused(self, url_path: str) -> str:
        with self.assertRaises(StaticPathError) as caught:
            resolve_static_path(url_path, ROOT)
        return str(caught.exception)

    def test_parent_segments_are_refused(self) -> None:
        for url_path in (
            "/../secret.html",
            "/a/../../b.js",
            "/assets/../../etc/passwd.html",
            "/./app.js",
            "/..",
        ):
            with self.subTest(url_path=url_path):
                message = self.assert_refused(url_path)
                self.assertIn("traversal", message)

    def test_percent_encoded_traversal_is_refused(self) -> None:
        for url_path in (
            "/%2e%2e/secret.html",
            "/a/%2e%2e/%2e%2e/b.js",
            "/%2E%2E/secret.html",
            "/%2e/app.js",
            "/a/%2e%2e%2fsecret.html",
        ):
            with self.subTest(url_path=url_path):
                message = self.assert_refused(url_path)
                self.assertTrue("traversal" in message or "backslash" in message)

    def test_absolute_and_drive_qualified_paths_are_refused(self) -> None:
        for url_path in (
            "//etc/passwd.html",
            "/C:/Windows/app.js",
            "/c:/windows/app.js",
            "file:///etc/passwd.html",
            "C:/Windows/app.js",
            "web/app.js",
        ):
            with self.subTest(url_path=url_path):
                message = self.assert_refused(url_path)
                self.assertTrue(
                    "absolute" in message
                    or "drive" in message
                    or "must be an absolute URL path" in message
                )

    def test_backslashes_and_control_characters_are_refused(self) -> None:
        for url_path in ("/a\\..\\b.js", "/app\x00.js", "/app\n.js", "/%00.html"):
            with self.subTest(url_path=url_path):
                message = self.assert_refused(url_path)
                self.assertTrue(
                    "backslash" in message or "control" in message
                )

    def test_a_non_allow_listed_extension_is_refused(self) -> None:
        for url_path in ("/routepilot.db", "/secrets.txt", "/app", "/../app.sqlite3"):
            with self.subTest(url_path=url_path):
                message = self.assert_refused(url_path)
                self.assertTrue(
                    "does not serve" in message or "traversal" in message
                )

    def test_refusals_name_the_offending_path(self) -> None:
        self.assertIn("/../secret.html", self.assert_refused("/../secret.html"))

    def test_the_resolved_candidate_always_stays_inside_the_root(self) -> None:
        for url_path in ("/app.js", "/a/b/c.css", "/deep/deeper/index.html"):
            request = resolve_static_path(url_path, ROOT)
            parts = request.candidate.parts
            self.assertEqual(parts[: len(ROOT.parts)], ROOT.parts)


if __name__ == "__main__":
    unittest.main()

