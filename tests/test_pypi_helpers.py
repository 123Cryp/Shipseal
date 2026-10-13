import json
import unittest
from unittest.mock import patch

from _bootstrap import m, gl, release_payload, index_payload, stamp, REPO, FakePyPI


class NameAndVersionNormalisation(unittest.TestCase):
    def test_canon_name_pep503(self):
        self.assertEqual(m._canon_name("My_Package.Name"), "my-package-name")
        self.assertEqual(m._canon_name("a--b__c..d"), "a-b-c-d")
        self.assertEqual(m._canon_name("  Widget "), "widget")

    def test_canon_version_trims_trailing_zero_segments(self):
        self.assertEqual(m._canon_version("1.0.0"), "1")
        self.assertEqual(m._canon_version("1.0"), "1")
        self.assertEqual(m._canon_version("1.2.0"), "1.2")
        self.assertEqual(m._canon_version("1.2.3"), "1.2.3")

    def test_canon_version_keeps_suffix_and_drops_leading_zeros(self):
        self.assertEqual(m._canon_version("2.0.0rc1"), "2rc1")
        self.assertEqual(m._canon_version("01.02"), "1.2")
        self.assertEqual(m._canon_version("1.0.post1"), "1.post1")

    def test_canon_version_of_garbage_is_lowercased_text(self):
        self.assertEqual(m._canon_version("Weird"), "weird")


class TimeMath(unittest.TestCase):
    def test_to_epoch_is_integer_seconds(self):
        self.assertEqual(m._to_epoch("1970-01-01T00:00:10Z"), 10)
        self.assertIsInstance(m._to_epoch("2024-05-01T12:00:00.987654Z"), int)

    def test_to_epoch_ignores_fraction_by_flooring(self):
        a = m._to_epoch("2024-05-01T12:00:00.999999Z")
        b = m._to_epoch("2024-05-01T12:00:00.000000Z")
        self.assertEqual(a, b)

    def test_to_epoch_naive_is_utc(self):
        self.assertEqual(m._to_epoch("1970-01-01T00:01:00"), 60)

    def test_to_epoch_offset_is_respected(self):
        self.assertEqual(m._to_epoch("1970-01-01T01:00:00+01:00"), 0)

    def test_to_epoch_garbage_is_none(self):
        self.assertIsNone(m._to_epoch("not a time"))
        self.assertIsNone(m._to_epoch(None))


class RepoMatching(unittest.TestCase):
    def test_key_strips_scheme_www_dotgit_and_case(self):
        self.assertEqual(m._repo_key("https://www.GitHub.com/Acme/Widget.git"), "github.com/acme/widget")
        self.assertEqual(m._repo_key("http://github.com/acme/widget/"), "github.com/acme/widget")

    def test_key_ignores_query_and_fragment(self):
        self.assertEqual(m._repo_key("https://github.com/acme/widget?tab=readme#top"), "github.com/acme/widget")

    def test_key_rejects_userinfo_ports_and_schemes(self):
        self.assertIsNone(m._repo_key("https://user:pw@github.com/acme/widget"))
        self.assertIsNone(m._repo_key("https://github.com:8443/acme/widget"))
        self.assertIsNone(m._repo_key("ftp://github.com/acme/widget"))
        self.assertIsNone(m._repo_key("git@github.com:acme/widget.git"))
        self.assertIsNone(m._repo_key("not a url"))

    def test_matches_exact_and_subpaths_only_at_segment_boundary(self):
        key = "github.com/acme/widget"
        self.assertTrue(m._repo_matches(key, "https://github.com/acme/widget"))
        self.assertTrue(m._repo_matches(key, "https://github.com/acme/widget/issues"))
        self.assertTrue(m._repo_matches(key, "https://github.com/ACME/Widget.git"))
        self.assertFalse(m._repo_matches(key, "https://github.com/acme/widget-evil"))
        self.assertFalse(m._repo_matches(key, "https://github.com/acme/widgets"))
        self.assertFalse(m._repo_matches(key, "https://github.com/acme"))
        self.assertFalse(m._repo_matches(key, "https://evil.example/github.com/acme/widget"))
        self.assertFalse(m._repo_matches(key, "https://github.com.evil.example/acme/widget"))

    def test_matches_empty_expected_never_matches(self):
        self.assertFalse(m._repo_matches("", "https://github.com/acme/widget"))


class EvidenceHostRules(unittest.TestCase):
    def test_host_extraction_requires_https_and_no_userinfo(self):
        self.assertEqual(m._evidence_host("https://pypi.org/pypi/x/json"), "pypi.org")
        self.assertEqual(m._evidence_host("http://pypi.org/pypi/x/json"), "")
        self.assertEqual(m._evidence_host("https://pypi.org@evil.example/"), "")
        self.assertEqual(m._evidence_host("https://pypi.org:bad/x"), "")

    def test_host_allowed_is_exact_or_subdomain(self):
        self.assertTrue(m._host_allowed("pypi.org", "pypi.org"))
        self.assertTrue(m._host_allowed("files.pypi.org", "pypi.org"))
        self.assertFalse(m._host_allowed("notpypi.org", "pypi.org"))
        self.assertFalse(m._host_allowed("pypi.org.evil.example", "pypi.org"))
        self.assertFalse(m._host_allowed("", "pypi.org"))

    def test_pypi_urls_are_built_from_anchors_only(self):
        release, index = m._pypi_urls("widget", "1.0.0")
        self.assertEqual(release, "https://pypi.org/pypi/widget/1.0.0/json")
        self.assertEqual(index, "https://pypi.org/pypi/widget/json")

    def test_pypi_urls_reject_path_and_authority_injection(self):
        for package, version in (("widget@evil.example", "1.0.0"), ("widget/../x", "1.0.0"),
                                 ("widget?x=1", "1.0.0"), ("widget#f", "1.0.0"), ("", "1.0.0"),
                                 ("widget", "1.0.0/../../x"), ("widget", "1.0 0"), ("widget", ""),
                                 ("Widget", "1.0.0")):
            with self.assertRaises(gl.vm.UserError, msg=(package, version)):
                m._pypi_urls(package, version)


class HttpAdapter(unittest.TestCase):
    def test_status_and_bytes_body_are_decoded(self):
        resp = gl.nondet.web.Response(200, "héllo".encode("utf-8"))
        with patch.object(gl.nondet.web, "request", return_value=resp):
            got = m._http_get("https://pypi.org/x")
        self.assertEqual(got, {"status": 200, "body": "héllo"})

    def test_404_is_reported_as_a_plain_value(self):
        with patch.object(gl.nondet.web, "request", return_value=gl.nondet.web.Response(404, b"nope")):
            self.assertEqual(m._http_get("https://pypi.org/x")["status"], 404)

    def test_exceptions_never_escape(self):
        with patch.object(gl.nondet.web, "request", side_effect=RuntimeError("boom")):
            self.assertEqual(m._http_get("https://pypi.org/x"), {"status": None, "body": ""})

    def test_exception_mentioning_404_is_a_404(self):
        with patch.object(gl.nondet.web, "request", side_effect=RuntimeError("HTTP 404 Not Found")):
            self.assertEqual(m._http_get("https://pypi.org/x")["status"], 404)

    def test_oversized_body_is_treated_as_unreadable(self):
        big = gl.nondet.web.Response(200, b"x" * (m.MAX_BODY_CHARS + 1))
        with patch.object(gl.nondet.web, "request", return_value=big):
            self.assertIsNone(m._http_get("https://pypi.org/x")["status"])

    def test_falls_back_to_render_when_request_is_missing(self):
        web = gl.nondet.web
        saved = web.request
        try:
            del type(web).request
            with patch.object(web, "render", return_value="body"):
                self.assertEqual(m._http_get("https://pypi.org/x"), {"status": 200, "body": "body"})
        finally:
            type(web).request = saved


class ReleaseExtraction(unittest.TestCase):
    def test_extracts_identity_files_and_urls(self):
        data = m._extract_release(json.dumps(release_payload()))
        self.assertEqual(data["name"], "widget")
        self.assertEqual(data["version"], "1.0.0")
        self.assertFalse(data["yanked"])
        self.assertEqual({f["packagetype"] for f in data["files"]}, {"bdist_wheel", "sdist"})
        self.assertIn(REPO, data["repo_urls"])
        self.assertTrue(all(isinstance(f["upload_ts"], int) for f in data["files"]))

    def test_volatile_fields_are_not_kept(self):
        a = m._extract_release(json.dumps(release_payload()))
        payload = release_payload()
        payload["last_serial"] = 99999
        for entry in payload["urls"]:
            entry["downloads"] = 12345
        b = m._extract_release(json.dumps(payload))
        self.assertEqual(m._canonical(a)[:0], m._canonical(b)[:0])
        self.assertEqual([f["sha256"] for f in a["files"]], [f["sha256"] for f in b["files"]])
        self.assertNotIn("last_serial", json.dumps(b))
        self.assertNotIn("downloads", json.dumps(b))

    def test_invalid_digest_becomes_empty(self):
        payload = release_payload(files=[("sdist", 0, "NOT-HEX")])
        data = m._extract_release(json.dumps(payload))
        self.assertEqual(data["files"][0]["sha256"], "")

    def test_digest_is_lowercased(self):
        payload = release_payload(files=[("sdist", 0, "A" * 64)])
        self.assertEqual(m._extract_release(json.dumps(payload))["files"][0]["sha256"], "a" * 64)

    def test_unparsable_or_wrongly_shaped_json_is_none(self):
        self.assertIsNone(m._extract_release("<html>nope</html>"))
        self.assertIsNone(m._extract_release("[]"))
        self.assertIsNone(m._extract_release(json.dumps({"info": {}})))
        self.assertIsNone(m._extract_release(json.dumps({"info": [], "urls": []})))

    def test_home_page_is_included_among_repo_urls(self):
        payload = release_payload(project_urls={}, home_page="https://github.com/acme/widget")
        self.assertEqual(m._extract_release(json.dumps(payload))["repo_urls"], ["https://github.com/acme/widget"])

    def test_description_is_whitespace_normalised_and_bounded(self):
        payload = release_payload(description="a\n\n  b " * 2000)
        data = m._extract_release(json.dumps(payload))
        self.assertLessEqual(len(data["description"]), 2000)
        self.assertNotIn("\n", data["description"])

    def test_file_count_and_list_sizes_are_bounded(self):
        payload = release_payload(files=[("sdist", 0, "a" * 64)] * 200)
        payload["info"]["requires_dist"] = ["x"] * 500
        data = m._extract_release(json.dumps(payload))
        self.assertLessEqual(len(data["files"]), 60)
        self.assertLessEqual(len(data["requires_dist"]), 50)

    def test_missing_upload_time_yields_none(self):
        payload = release_payload()
        for entry in payload["urls"]:
            entry.pop("upload_time_iso_8601")
        data = m._extract_release(json.dumps(payload))
        self.assertTrue(all(f["upload_ts"] is None for f in data["files"]))


class IndexProbe(unittest.TestCase):
    def setUp(self):
        self.url = "https://pypi.org/pypi/widget/json"

    def probe(self, status, body, version="1.0.0"):
        fake = FakePyPI()
        fake.routes[self.url] = (status, body if isinstance(body, str) else json.dumps(body))
        with fake.patch():
            return m._probe_index(self.url, "widget", version)

    def test_index_without_the_version(self):
        got = self.probe(200, index_payload(versions=("0.1.0",)))
        self.assertEqual(got, {"status": "ok", "name_ok": True, "has_version": False})

    def test_index_with_the_version(self):
        self.assertTrue(self.probe(200, index_payload(versions=("1.0.0",)))["has_version"])

    def test_index_with_an_equivalent_version_counts_as_present(self):
        self.assertTrue(self.probe(200, index_payload(versions=("1.0",)))["has_version"])

    def test_index_for_a_different_project_name(self):
        self.assertFalse(self.probe(200, index_payload(name="other"))["name_ok"])

    def test_index_404_and_server_error_and_garbage(self):
        self.assertEqual(self.probe(404, "x")["status"], "not_found")
        self.assertEqual(self.probe(503, "x")["status"], "error")
        self.assertEqual(self.probe(200, "<html>")["status"], "error")
        self.assertEqual(self.probe(200, {"info": {}})["status"], "error")


class FactsCollection(unittest.TestCase):
    def test_existing_release_does_not_touch_the_index(self):
        fake = FakePyPI().release().index()
        with fake.patch():
            facts = m._collect_facts("widget", "1.0.0")
        self.assertEqual(facts["release"]["status"], "ok")
        self.assertEqual(facts["index"], {"status": "skipped"})
        self.assertEqual(fake.calls, [fake.release_url])

    def test_404_triggers_the_index_probe(self):
        fake = FakePyPI().missing().index()
        with fake.patch():
            facts = m._collect_facts("widget", "1.0.0")
        self.assertEqual(facts["release"]["status"], "not_found")
        self.assertEqual(facts["index"]["status"], "ok")
        self.assertEqual(fake.calls, [fake.release_url, fake.index_url])

    def test_server_error_is_an_error_status_not_a_404(self):
        fake = FakePyPI().release("oops", status=500)
        with fake.patch():
            facts = m._collect_facts("widget", "1.0.0")
        self.assertEqual(facts["release"]["status"], "error")
        self.assertEqual(facts["index"], {"status": "skipped"})

    def test_network_failure_is_an_error_status(self):
        fake = FakePyPI().release("x", status=None)
        with fake.patch():
            self.assertEqual(m._collect_facts("widget", "1.0.0")["release"]["status"], "error")

    def test_unparsable_200_is_an_error_status(self):
        fake = FakePyPI().release("<html>captive portal</html>")
        with fake.patch():
            self.assertEqual(m._collect_facts("widget", "1.0.0")["release"]["status"], "error")


class FrozenPages(unittest.TestCase):
    def facts(self):
        fake = FakePyPI().release().index()
        with fake.patch():
            return m._collect_facts("widget", "1.0.0")

    def spec(self):
        return {"package": "widget", "version": "1.0.0"}

    def test_pages_round_trip_to_the_same_facts(self):
        facts = self.facts()
        pages = m._pages_from_facts(facts)
        back = m._facts_from_pages(pages, self.spec())
        self.assertEqual(back["release"], facts["release"])
        self.assertEqual(back["index"], facts["index"])

    def test_root_commits_to_the_manifest_and_detects_tampering(self):
        pages = m._pages_from_facts(self.facts())
        root = m._evidence_root(pages)
        self.assertTrue(m._frozen_intact(pages, root))
        pages[0]["content"] = pages[0]["content"].replace("widget", "gadget")
        self.assertFalse(m._frozen_intact(pages, root))

    def test_wrong_root_or_wrong_page_count_is_not_intact(self):
        pages = m._pages_from_facts(self.facts())
        self.assertFalse(m._frozen_intact(pages, "0" * 64))
        self.assertFalse(m._frozen_intact(pages[:1], m._evidence_root(pages[:1])))
        self.assertFalse(m._frozen_intact([], m._evidence_root([])))

    def test_unparsable_frozen_content_gives_no_facts(self):
        pages = m._pages_from_facts(self.facts())
        pages[0]["content"] = "not json"
        self.assertIsNone(m._facts_from_pages(pages, self.spec()))

    def test_stamp_helper_is_pypi_shaped(self):
        self.assertRegex(stamp(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")


if __name__ == "__main__":
    unittest.main()
