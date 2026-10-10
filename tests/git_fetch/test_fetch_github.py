"""네트워크 없이 URL 검증과 commit 고정, 원문 복원을 확인한다."""

import base64
import io
import json
import unittest
from unittest.mock import patch

from fetch_github import fetch_file


class FetchGithubTest(unittest.TestCase):
    @patch("fetch_github.urlopen")
    def test_fetch_pins_commit_and_decodes_korean_text(self, urlopen):
        text = "# 수업 개념\nprint('hello')\n"
        commit = "a" * 40
        file = {"type": "file", "path": "수업/example.py", "encoding": "base64",
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        urlopen.side_effect = [
            io.BytesIO(json.dumps([{"sha": commit}]).encode()),
            io.BytesIO(json.dumps(file).encode()),
        ]
        result = fetch_file("https://github.com/example/course.git", file["path"], "lesson/one")
        first, second = [call.args[0] for call in urlopen.call_args_list]
        self.assertIn("sha=lesson%2Fone", first.full_url)
        self.assertIn(f"ref={commit}", second.full_url)
        self.assertIn("%EC%88%98%EC%97%85/example.py", second.full_url)
        self.assertEqual(result["text"], text)
        self.assertEqual(result["repo"], "example/course")
        self.assertIn(f"/blob/{commit}/", result["source_url"])

    @patch("fetch_github.urlopen")
    def test_invalid_inputs_do_not_make_requests(self, urlopen):
        for url, path in [
            ("https://example.com/owner/repo", "README.md"),
            ("https://github.com/owner/repo/tree/main", "README.md"),
            ("https://github.com/owner/repo", "../README.md"),
            ("https://github.com/owner/repo", "/README.md"),
        ]:
            with self.subTest(url=url, path=path), self.assertRaises(ValueError):
                fetch_file(url, path)
        urlopen.assert_not_called()

    @patch("fetch_github.api_json", side_effect=[[{"sha": "a" * 40}], []])
    def test_directory_is_rejected(self, api_json):
        with self.assertRaisesRegex(ValueError, "일반 파일"):
            fetch_file("https://github.com/example/course", "docs")


if __name__ == "__main__":
    unittest.main()
