"""Zestaw T1-T6 dla synca arkadia-mpackage -> packages.mudlet.org.

T1 parser sources.json, T2 detekcja zmian, T3 build wariantu,
T4 idempotentnosc (end-to-end na atrapsze IO), T5 kontrakt publish,
T6 meta-bezpieczenstwo (skan wzorcow wrazliwych).

Wszystko na unittest ze stdlib; fixture'y w tests/fixtures to nagrane
odpowiedzi publicznego API GitHub i bajty assetow release'owych zrodel.
Zadne wywolanie sieciowe nie wychodzi z testow (warstwa IO wstrzykiwana).

Uwaga: wzorce wrazliwe skladane sa z fragmentow w runtime, zeby ich literaly
nigdy nie pojawily sie w repozytorium (meta-test T6 skanuje tez ten plik).
"""

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts import make_variant, sync  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"

PACKAGES = [
    {
        "mpackage": "ishtar_cal",
        "source_repo": "Isithunzi000/arkadia-mudlet-calendar_ishtar",
        "asset": "ishtar_cal.mpackage",
        "created": "2026-08-20T11:13:44+00:00",
        "version": "1.8.21m",
        "xml": "ishtar_cal.xml",
        "fixture_asset": "ishtar_cal.mpackage",
        "fixture_release": "release_ishtar.json",
    },
    {
        "mpackage": "imperium_cal",
        "source_repo": "Isithunzi000/arkadia-mudlet-calendar_imperium",
        "asset": "imperium_cal.mpackage",
        "created": "2026-08-20T11:13:48+00:00",
        "version": "1.8.22m",
        "xml": "imperium_cal.xml",
        "fixture_asset": "imperium_cal.mpackage",
        "fixture_release": "release_imperium.json",
    },
    {
        "mpackage": "pasek_kalendarz_arkadia",
        "source_repo": "Isithunzi000/arkadia-mudlet-pasek_czas",
        "asset": "pasek_kalendarz_arkadia.mpackage",
        "created": "2026-08-20T11:03:17+00:00",
        "version": "1.6.6",
        "xml": "pasek_kalendarz_arkadia.xml",
        "fixture_asset": "pasek_kalendarz_arkadia.mpackage",
        "fixture_release": "release_pasek.json",
    },
]

PUBLISH_OK = {
    "success": True,
    "pullRequest": "https://github.com/Mudlet/mudlet-package-repository/pull/1",
}


def publish_queue(*statuses):
    """Kolejka odpowiedzi publish: 200 dostaje pelne body, reszta error."""
    out = []
    for status in statuses:
        body = dict(PUBLISH_OK) if status == 200 else {"error": f"HTTP {status}"}
        out.append((status, body))
    return out


class FakeOps:
    """Atrapa warstwy IO synca: nagrane odpowiedzi, zero sieci."""

    def __init__(self, publish_responses=None):
        self.releases = {}          # repo zrodlowe -> nagrany JSON latest release
        self.downloads = {}         # browser_download_url -> bajty asseta
        self.variant_releases = {}  # tag -> {nazwa asseta: bajty}
        self.created_releases = []  # [(tag, nazwa asseta)]
        self.publish_responses = list(publish_responses or [])
        self.publish_calls = []     # [{"token":..., "payload":...}]
        self.oidc_calls = []        # [audience]

    def get_latest_release(self, repo):
        return self.releases[repo]

    def download(self, url):
        return self.downloads[url]

    def find_release_asset(self, tag, asset_name):
        rel = self.variant_releases.get(tag)
        return rel.get(asset_name) if rel else None

    def create_release(self, tag, asset_name, asset_bytes, body=""):
        self.created_releases.append((tag, asset_name))
        self.variant_releases.setdefault(tag, {})[asset_name] = asset_bytes

    def request_oidc_token(self, audience):
        self.oidc_calls.append(audience)
        return "fake-oidc-token"

    def post_publish(self, token, payload):
        self.publish_calls.append({"token": token, "payload": payload})
        if not self.publish_responses:
            raise AssertionError("nieoczekiwane wywolanie publish")
        return self.publish_responses.pop(0)


def make_ops(publish_responses=None):
    """FakeOps zaladowany fixture'ami wszystkich trzech zrodel."""
    ops = FakeOps(publish_responses)
    for pkg in PACKAGES:
        release = json.loads((FIXTURES / pkg["fixture_release"]).read_text(encoding="utf-8"))
        ops.releases[pkg["source_repo"]] = release
        asset_url = next(
            a["browser_download_url"] for a in release["assets"] if a["name"] == pkg["asset"]
        )
        ops.downloads[asset_url] = (FIXTURES / pkg["fixture_asset"]).read_bytes()
    return ops


def read_member(zip_bytes, name):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.read(name)


def variant_bytes(pkg):
    asset = (FIXTURES / pkg["fixture_asset"]).read_bytes()
    return make_variant.build_variant(asset, pkg["created"])


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sync-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = self.tmp / "state.json"

    def run_sync(self, ops, only_package=None):
        return sync.run(REPO_ROOT / "sources.json", self.state_path, ops,
                        only_package=only_package)

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- T1: sources

class T1Sources(SyncTestBase):
    def entry(self, **over):
        e = {
            "mpackage": "x_cal",
            "source_repo": "Isithunzi000/x",
            "asset": "x_cal.mpackage",
            "created": "2026-08-20T11:13:44+00:00",
        }
        e.update(over)
        return e

    def write_sources(self, obj):
        path = self.tmp / "sources.json"
        text = obj if isinstance(obj, str) else json.dumps(obj)
        path.write_text(text, encoding="utf-8")
        return path

    def test_load_repo_sources_ok(self):
        sources = sync.load_sources(REPO_ROOT / "sources.json")
        self.assertEqual(
            [s.mpackage for s in sources],
            ["ishtar_cal", "imperium_cal", "pasek_kalendarz_arkadia"],
        )
        for s in sources:
            self.assertTrue(s.source_repo.startswith("Isithunzi000/"))
            self.assertEqual(s.asset, f"{s.mpackage}.mpackage")

    def test_duplicate_mpackage_rejected(self):
        path = self.write_sources({"packages": [self.entry(), self.entry()]})
        with self.assertRaises(ValueError):
            sync.load_sources(path)

    def test_missing_field_rejected(self):
        e = self.entry()
        del e["asset"]
        path = self.write_sources({"packages": [e]})
        with self.assertRaises(ValueError):
            sync.load_sources(path)

    def test_bad_created_format_rejected(self):
        path = self.write_sources({"packages": [self.entry(created="2026-08-20")]})
        with self.assertRaises(ValueError):
            sync.load_sources(path)

    def test_invalid_json_rejected(self):
        path = self.write_sources("{ not json")
        with self.assertRaises(ValueError):
            sync.load_sources(path)

    def test_asset_must_match_mpackage(self):
        path = self.write_sources({"packages": [self.entry(asset="inny.mpackage")]})
        with self.assertRaises(ValueError):
            sync.load_sources(path)


# ------------------------------------------------------- T2: detekcja zmian

class T2Detection(unittest.TestCase):
    def entry(self, version="1.8.21m", status="published"):
        return {
            "source_version": version,
            "variant_tag": f"ishtar_cal-v{version}",
            "variant_sha256": "a" * 64,
            "publish": {"status": status},
        }

    def test_first_run_empty_state_full(self):
        self.assertEqual(sync.decide(None, "1.8.21m"), "full")

    def test_same_version_published_skip(self):
        self.assertEqual(sync.decide(self.entry(), "1.8.21m"), "skip")

    def test_same_version_pending_publish_only(self):
        self.assertEqual(
            sync.decide(self.entry(status="pending"), "1.8.21m"), "publish_only")

    def test_same_version_failed_publish_only(self):
        self.assertEqual(
            sync.decide(self.entry(status="failed"), "1.8.21m"), "publish_only")

    def test_new_version_full(self):
        self.assertEqual(sync.decide(self.entry("1.8.20m"), "1.8.21m"), "full")


# ------------------------------------------------------ T3: build wariantu

class T3VariantBuild(unittest.TestCase):
    def test_created_added_for_all_packages(self):
        for pkg in PACKAGES:
            config = read_member(variant_bytes(pkg), "config.lua").decode("utf-8")
            self.assertIn(f'created = "{pkg["created"]}"', config, pkg["mpackage"])

    def test_six_required_fields_present(self):
        for pkg in PACKAGES:
            config = read_member(variant_bytes(pkg), "config.lua").decode("utf-8")
            self.assertEqual(make_variant.missing_fields(config), [], pkg["mpackage"])

    def test_xml_byte_identical(self):
        for pkg in PACKAGES:
            asset = (FIXTURES / pkg["fixture_asset"]).read_bytes()
            self.assertEqual(
                read_member(variant_bytes(pkg), pkg["xml"]),
                read_member(asset, pkg["xml"]),
                pkg["mpackage"],
            )

    def test_only_config_changes(self):
        for pkg in PACKAGES:
            asset = (FIXTURES / pkg["fixture_asset"]).read_bytes()
            variant = variant_bytes(pkg)
            with zipfile.ZipFile(io.BytesIO(asset)) as za, \
                    zipfile.ZipFile(io.BytesIO(variant)) as zv:
                self.assertEqual(sorted(za.namelist()), sorted(zv.namelist()))
                for name in za.namelist():
                    if name != "config.lua":
                        self.assertEqual(za.read(name), zv.read(name), name)

    def test_deterministic_double_build(self):
        for pkg in PACKAGES:
            asset = (FIXTURES / pkg["fixture_asset"]).read_bytes()
            first = make_variant.build_variant(asset, pkg["created"])
            second = make_variant.build_variant(asset, pkg["created"])
            self.assertEqual(first, second, pkg["mpackage"])

    def test_refuses_config_with_created(self):
        pkg = PACKAGES[0]
        once = variant_bytes(pkg)
        with self.assertRaises(ValueError):
            make_variant.build_variant(once, pkg["created"])

    def test_names(self):
        self.assertEqual(
            make_variant.variant_tag("ishtar_cal", "1.8.21m"), "ishtar_cal-v1.8.21m")
        self.assertEqual(
            make_variant.variant_asset_name("ishtar_cal"), "ishtar_cal.mpackage")


# ------------------------------------------------------ T4: idempotentnosc

class T4Idempotency(SyncTestBase):
    def test_first_run_builds_and_publishes_all(self):
        ops = make_ops(publish_queue(200, 200, 200))
        self.run_sync(ops)
        self.assertEqual(len(ops.created_releases), 3)
        self.assertEqual(len(ops.publish_calls), 3)
        state = self.read_state()
        for pkg in PACKAGES:
            entry = state[pkg["mpackage"]]
            self.assertEqual(entry["source_version"], pkg["version"])
            self.assertEqual(entry["variant_tag"], f'{pkg["mpackage"]}-v{pkg["version"]}')
            self.assertEqual(entry["publish"]["status"], "published")
            self.assertEqual(entry["publish"]["pr_url"], PUBLISH_OK["pullRequest"])
            expected_sha = hashlib.sha256(variant_bytes(pkg)).hexdigest()
            self.assertEqual(entry["variant_sha256"], expected_sha)

    def test_second_run_is_noop(self):
        self.run_sync(make_ops(publish_queue(200, 200, 200)))
        ops2 = make_ops()  # pusta kolejka: kazde publish wybuchnie w atrapie
        self.run_sync(ops2)
        self.assertEqual(ops2.created_releases, [])
        self.assertEqual(ops2.publish_calls, [])
        self.assertEqual(ops2.oidc_calls, [])

    def test_pending_retries_publish_only(self):
        self.run_sync(make_ops(publish_queue(403, 403, 403)))
        state = self.read_state()
        for pkg in PACKAGES:
            self.assertEqual(state[pkg["mpackage"]]["publish"]["status"], "pending")
        ops2 = make_ops(publish_queue(200, 200, 200))
        self.run_sync(ops2)
        self.assertEqual(ops2.created_releases, [])  # warianty nie przebudowane
        self.assertEqual(len(ops2.publish_calls), 3)
        state2 = self.read_state()
        for pkg in PACKAGES:
            self.assertEqual(state2[pkg["mpackage"]]["publish"]["status"], "published")

    def test_crash_resume_recognises_existing_variant(self):
        # Release wariantu powstal, ale state.json nie zostal zacommitowany:
        # sync ma uznac istniejacy asset za zbudowany i isc dalej.
        ops = make_ops(publish_queue(200, 200, 200))
        for pkg in PACKAGES:
            tag = make_variant.variant_tag(pkg["mpackage"], pkg["version"])
            name = make_variant.variant_asset_name(pkg["mpackage"])
            ops.variant_releases[tag] = {name: variant_bytes(pkg)}
        self.run_sync(ops)
        self.assertEqual(ops.created_releases, [])
        self.assertEqual(len(ops.publish_calls), 3)
        self.assertEqual(self.read_state()["ishtar_cal"]["publish"]["status"], "published")

    def test_failed_publish_keeps_error(self):
        ops = make_ops(publish_queue(409, 200, 200))
        self.run_sync(ops)
        state = self.read_state()
        self.assertEqual(state["ishtar_cal"]["publish"]["status"], "failed")
        self.assertIn("error", state["ishtar_cal"]["publish"])
        self.assertEqual(state["imperium_cal"]["publish"]["status"], "published")
        self.assertEqual(state["pasek_kalendarz_arkadia"]["publish"]["status"], "published")

    def test_only_package_filter(self):
        ops = make_ops(publish_queue(200))
        self.run_sync(ops, only_package="imperium_cal")
        self.assertEqual(ops.created_releases, [("imperium_cal-v1.8.22m", "imperium_cal.mpackage")])
        self.assertEqual(len(ops.publish_calls), 1)
        self.assertEqual(list(self.read_state().keys()), ["imperium_cal"])

    def test_state_file_sorted_with_trailing_newline(self):
        self.run_sync(make_ops(publish_queue(200, 200, 200)))
        raw = self.state_path.read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(raw, sync.dump_state(json.loads(raw)))
        self.assertEqual(list(json.loads(raw).keys()), sorted(json.loads(raw).keys()))


# -------------------------------------------------- T5: kontrakt publish

class T5PublishContract(SyncTestBase):
    REPO = "Isithunzi000/arkadia-mpackage"

    def test_artifact_url_shape(self):
        url = sync.build_artifact_url(self.REPO, "ishtar_cal-v1.8.21m", "ishtar_cal.mpackage")
        self.assertEqual(
            url,
            "https://github.com/Isithunzi000/arkadia-mpackage/"
            "releases/download/ishtar_cal-v1.8.21m/ishtar_cal.mpackage",
        )

    def test_assert_belongs_accepts_own(self):
        for pkg in PACKAGES:
            url = sync.build_artifact_url(
                self.REPO, f'{pkg["mpackage"]}-v{pkg["version"]}', pkg["asset"])
            sync.assert_artifact_belongs(url, self.REPO)  # bez wyjatku

    def test_assert_belongs_rejects_foreign(self):
        bad = [
            "http://github.com/Isithunzi000/arkadia-mpackage/releases/download/t/f",
            "https://evil.com/Isithunzi000/arkadia-mpackage/releases/download/t/f",
            "https://github.com/Isithunzi000/inne-repo/releases/download/t/f",
            "https://github.com/Isithunzi000/arkadia-mpackage/raw/main/f",
            "not-a-url",
        ]
        for url in bad:
            with self.assertRaises(ValueError, msg=url):
                sync.assert_artifact_belongs(url, self.REPO)

    def test_status_mapping(self):
        self.assertEqual(sync.publish_status_for(200), "published")
        self.assertEqual(sync.publish_status_for(403), "pending")
        for code in (400, 409, 500, 503):
            self.assertEqual(sync.publish_status_for(code), "failed", code)

    def test_publish_payload_is_artifact_url_only(self):
        ops = make_ops(publish_queue(200))
        self.run_sync(ops, only_package="ishtar_cal")
        payload = ops.publish_calls[0]["payload"]
        self.assertEqual(set(payload.keys()), {"artifactUrl"})
        self.assertTrue(payload["artifactUrl"].startswith(
            "https://github.com/Isithunzi000/arkadia-mpackage/releases/download/"))

    def test_oidc_audience(self):
        ops = make_ops(publish_queue(200))
        self.run_sync(ops, only_package="ishtar_cal")
        self.assertEqual(ops.oidc_calls, ["https://packages.mudlet.org"])


# --------------------------------------------- T6: meta-bezpieczenstwo

def assembled(*parts):
    return "".join(parts)


class T6SecurityScan(unittest.TestCase):
    def test_scan_finds_pat(self):
        pat = assembled("github_", "pat_", "ABC123")
        self.assertTrue(sync.scan_for_sensitive(f"token = {pat}"))

    def test_scan_case_insensitive(self):
        pat = assembled("GITHUB_", "PAT_", "x")
        self.assertTrue(sync.scan_for_sensitive(pat))

    def test_scan_finds_profanity(self):
        words = [
            assembled("kur", "wa"), assembled("ch", "uj"), assembled("pi", "zda"),
            assembled("je", "bany"), assembled("du", "pa"),
        ]
        for word in words:
            self.assertTrue(sync.scan_for_sensitive(f"tekst {word} tekst"), word)

    def test_scan_clean_text(self):
        self.assertEqual(
            sync.scan_for_sensitive("zwykly opis pakietu, wersja 1.8.21m"), [])

    def test_repo_files_clean(self):
        offenders = []
        for path in sorted(REPO_ROOT.rglob("*")):
            if ".git" in path.parts or path.is_dir():
                continue
            if path.suffix in (".mpackage", ".zip"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = sync.scan_for_sensitive(text)
            if hits:
                offenders.append((str(path.relative_to(REPO_ROOT)), hits))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
