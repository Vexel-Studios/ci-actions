import json
import unittest

from vexel_ci.github_api import GitHubApi, GitHubApiError


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.status = 200
        self.headers = headers or {}
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self._payload


class GitHubApiTests(unittest.TestCase):
    def test_callable_token_and_name_identity_paginate_variable_collection(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if "page=2" in request.full_url:
                return FakeResponse({"total_count": 2, "variables": [{"name": "CI_LIGHT_RUNS_ON", "value": "[]"}]})
            return FakeResponse(
                {"total_count": 2, "variables": [{"name": "CI_RUNS_ON", "value": "[]"}]},
                {"Link": '<https://api.github.com/repos/o/r/actions/variables?per_page=100&page=2>; rel="next"'},
            )

        api = GitHubApi(lambda: "installation-token", opener=opener)
        result = api.paginated("/repos/o/r/actions/variables", "variables", identity_key="name")

        self.assertEqual(["CI_RUNS_ON", "CI_LIGHT_RUNS_ON"], [item["name"] for item in result])
        self.assertEqual(2, len(requests))
        self.assertEqual("Bearer installation-token", requests[0].get_header("Authorization"))

    def test_rejects_missing_identity_in_paginated_collection(self):
        api = GitHubApi(
            "token",
            opener=lambda _request, **_kwargs: FakeResponse(
                {"total_count": 1, "variables": [{"value": "[]"}]}
            ),
        )

        with self.assertRaisesRegex(GitHubApiError, "name.*identity"):
            api.paginated("/repos/o/r/actions/variables", "variables", identity_key="name")

    def test_rejects_incomplete_pagination(self):
        api = GitHubApi(
            "token",
            opener=lambda _request, **_kwargs: FakeResponse(
                {"total_count": 2, "variables": [{"name": "ONLY_ONE", "value": "[]"}]}
            ),
        )

        with self.assertRaisesRegex(GitHubApiError, "incomplete"):
            api.paginated("/repos/o/r/actions/variables", "variables", identity_key="name")


if __name__ == "__main__":
    unittest.main()
