"""Sync pakietow z release'ow zrodlowych do packages.mudlet.org.

Fazy (per pakiet z sources.json):
  1. odczyt najnowszego release'u zrodla (publiczne API GitHub),
  2. nowa wersja -> pobranie asseta, build wariantu (+ created), bramki,
  3. release wariantu w tym repo (crash-resume: istniejacy asset o zgodnym
     SHA uznawany jest za zbudowany),
  4. publikacja: OIDC (aud packages.mudlet.org) -> POST /api/publish,
  5. zapis stanu do state.json (idempotentnosc, wznowienia).

Warstwa IO (Ops) jest wstrzykiwana: produkcyjnie RealOps (urllib), w testach
atrapa na fixture'ach — zadna logika nie zalezy od sieci.

Uruchomienie z repo root:  python3 -m scripts.sync
Zmienne srodowiskowe: GITHUB_TOKEN (operacje na repo), opcjonalnie
ONLY_PACKAGE (ograniczenie runu do jednego mpackage). OIDC biezacego joba
opisuje env runnera: ACTIONS_ID_TOKEN_REQUEST_URL / _TOKEN.
"""

import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from scripts import make_variant

TARGET_REPO = "Isithunzi000/arkadia-mpackage"
OIDC_AUDIENCE = "https://packages.mudlet.org"
PUBLISH_URL = "https://packages.mudlet.org/api/publish"
GITHUB_API = "https://api.github.com"
USER_AGENT = "arkadia-mpackage-sync"

SOURCE_FIELDS = ("mpackage", "source_repo", "asset", "created")


@dataclass
class PackageSource:
    mpackage: str
    source_repo: str
    asset: str
    created: str


# ------------------------------------------------------------- sources/state

def load_sources(path):
    """Czyta i waliduje sources.json; kazdy defekt to ValueError."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"sources.json nieczytelny: {exc}") from exc
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list) or not packages:
        raise ValueError("sources.json: brak niepustej listy packages")
    seen = set()
    out = []
    for i, entry in enumerate(packages):
        if not isinstance(entry, dict):
            raise ValueError(f"sources.json: wpis {i} nie jest obiektem")
        for field in SOURCE_FIELDS:
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise ValueError(f"sources.json: wpis {i} bez pola {field}")
        if entry["mpackage"] in seen:
            raise ValueError(f"sources.json: duplikat mpackage {entry['mpackage']}")
        seen.add(entry["mpackage"])
        if not make_variant.CREATED_RE.match(entry["created"]):
            raise ValueError(
                f"sources.json: zly format created w {entry['mpackage']}")
        expected = make_variant.variant_asset_name(entry["mpackage"])
        if entry["asset"] != expected:
            raise ValueError(
                f"sources.json: asset musi byc {expected} (wpis {entry['mpackage']})")
        out.append(PackageSource(**{f: entry[f] for f in SOURCE_FIELDS}))
    return out


def load_state(path):
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def dump_state(state):
    """Deterministyczny zapis: klucze sortowane, LF, koncowy newline."""
    return json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------ detekcja

def version_from_tag(tag):
    return tag[1:] if tag.startswith("v") else tag


def decide(entry, latest_version):
    """full = build+publish, publish_only = sam retry publikacji, skip = nic."""
    if entry is None or entry.get("source_version") != latest_version:
        return "full"
    if entry.get("publish", {}).get("status") == "published":
        return "skip"
    return "publish_only"


# --------------------------------------------------------- kontrakt publish

def build_artifact_url(repo, tag, filename):
    return f"https://github.com/{repo}/releases/download/{tag}/{filename}"


def assert_artifact_belongs(url, repo):
    """Lustro assertArtifactBelongsToRun z packages.mudlet.org."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "") != "github.com":
        raise ValueError(f"artifactUrl poza https://github.com: {url}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 5 or parts[2] != "releases" or parts[3] != "download":
        raise ValueError(f"artifactUrl nie jest release assetem: {url}")
    if f"{parts[0]}/{parts[1]}".lower() != repo.lower():
        raise ValueError(f"artifactUrl wskazuje inne repo niz {repo}: {url}")


def publish_status_for(http_status):
    if http_status == 200:
        return "published"
    if http_status == 403:
        return "pending"
    return "failed"


# -------------------------------------------------------- meta-bezpieczenstwo

def _sensitive_patterns():
    # Wzorce skladane z fragmentow: ich literaly nie moga pojawic sie w repo.
    fragments = [("github_", "pat_"), ("kur", "w"), ("ch", "uj"),
                 ("pi", "zd"), ("je", "b"), ("du", "pa")]
    return [a + b for a, b in fragments]


def scan_for_sensitive(text):
    lowered = text.lower()
    return [p for p in _sensitive_patterns() if p in lowered]


# ---------------------------------------------------------------------- bramki

def _asset_url(latest, src):
    for asset in latest.get("assets", []):
        if asset.get("name") == src.asset:
            return asset["browser_download_url"]
    raise ValueError(
        f"brak asseta {src.asset} w release {latest.get('tag_name')} ({src.source_repo})")


def _gate_payload_equal(asset_bytes, variant_bytes):
    """Kazdy plik poza config.lua musi byc bajtowo identyczny jak w zrodle."""
    original = make_variant.read_members(asset_bytes)
    variant = make_variant.read_members(variant_bytes)
    if set(original) != set(variant):
        raise ValueError("wariant zmienia liste plikow pakietu")
    for name in original:
        if name != "config.lua" and original[name] != variant[name]:
            raise ValueError(f"wariant zmienia zawartosc {name}")


# -------------------------------------------------------------------- sync

def sync_package(src, state, ops):
    """Jeden pakiet przez fazy synca; zwraca wpis do raportu."""
    latest = ops.get_latest_release(src.source_repo)
    version = version_from_tag(latest["tag_name"])
    entry = state.get(src.mpackage)
    action = decide(entry, version)
    if action == "skip":
        return {"action": "skip", "publish": "published"}

    if action == "full":
        asset = ops.download(_asset_url(latest, src))
        variant = make_variant.build_variant(asset, src.created)
        if make_variant.build_variant(asset, src.created) != variant:
            raise ValueError(f"{src.mpackage}: build wariantu niedeterministyczny")
        _gate_payload_equal(asset, variant)
        hits = scan_for_sensitive(f'created = "{src.created}"')
        if hits:
            raise ValueError(f"{src.mpackage}: skan dodanej linii: {hits}")

        tag = make_variant.variant_tag(src.mpackage, version)
        name = make_variant.variant_asset_name(src.mpackage)
        variant_sha = sha256(variant).hexdigest()
        existing = ops.find_release_asset(tag, name)
        if existing is None:
            ops.create_release(
                tag, name, variant,
                body=f"Wariant {src.mpackage} {version} dla packages.mudlet.org "
                     f"(+ created w config.lua; reszta bajtowo jak w zrodle).")
        elif sha256(existing).hexdigest() != variant_sha:
            raise ValueError(
                f"{src.mpackage}: tag {tag} istnieje z innym SHA assetu — "
                "konflikt wymaga decyzji czlowieka")

        entry = {
            "source_version": version,
            "variant_tag": tag,
            "variant_sha256": variant_sha,
            "publish": {"status": "built"},
        }
        state[src.mpackage] = entry

    url = build_artifact_url(
        TARGET_REPO, entry["variant_tag"],
        make_variant.variant_asset_name(src.mpackage))
    assert_artifact_belongs(url, TARGET_REPO)
    token = ops.request_oidc_token(OIDC_AUDIENCE)
    status, body = ops.post_publish(token, {"artifactUrl": url})
    mapped = publish_status_for(status)
    publish = {"status": mapped}
    if mapped == "published":
        publish["pr_url"] = body.get("pullRequest", "")
    elif mapped == "failed":
        publish["error"] = f"HTTP {status}: {body.get('error', '')}"
    entry["publish"] = publish
    return {"action": action, "publish": mapped}


def run(sources_path, state_path, ops, only_package=None):
    """Pelny run synca; state.json zapisywany po kazdym pakiecie (wznowienia)."""
    sources = load_sources(sources_path)
    state = load_state(state_path)
    written = dump_state(state)
    report = {}
    for src in sources:
        if only_package and src.mpackage != only_package:
            continue
        try:
            report[src.mpackage] = sync_package(src, state, ops)
        finally:
            current = dump_state(state)
            if current != written:
                Path(state_path).write_text(current, encoding="utf-8")
                written = current
    return report


# -------------------------------------------------------------- produkcyjne IO

class RealOps:
    """Warstwa IO na urllib: publiczne odczyty zrodel, GITHUB_TOKEN dla tego
    repo, OIDC runnera dla publikacji."""

    def __init__(self, github_token):
        self.github_token = github_token

    @staticmethod
    def _get(url, token=None):
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": USER_AGENT})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req) as resp:
            return resp.read()

    @staticmethod
    def _send(req):
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get_latest_release(self, repo):
        return json.loads(self._get(f"{GITHUB_API}/repos/{repo}/releases/latest"))

    def download(self, url):
        return self._get(url)

    def find_release_asset(self, tag, asset_name):
        url = f"{GITHUB_API}/repos/{TARGET_REPO}/releases/tags/{tag}"
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": USER_AGENT,
                          "Authorization": f"Bearer {self.github_token}"})
        status, body = self._send(req)
        if status == 404:
            return None
        if status != 200:
            raise ValueError(f"odczyt release {tag}: HTTP {status}")
        for asset in json.loads(body).get("assets", []):
            if asset.get("name") == asset_name:
                return self._get(asset["browser_download_url"])
        return None

    def create_release(self, tag, asset_name, asset_bytes, body=""):
        payload = json.dumps({"tag_name": tag, "name": tag, "body": body}).encode()
        req = urllib.request.Request(
            f"{GITHUB_API}/repos/{TARGET_REPO}/releases", data=payload, method="POST",
            headers={"Accept": "application/vnd.github+json",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT,
                     "Authorization": f"Bearer {self.github_token}"})
        status, raw = self._send(req)
        if status != 201:
            raise ValueError(f"create release {tag}: HTTP {status}: {raw[:200]!r}")
        upload_url = json.loads(raw)["upload_url"].split("{")[0]
        upload_url += f"?name={urllib.parse.quote(asset_name)}"
        req = urllib.request.Request(
            upload_url, data=asset_bytes, method="POST",
            headers={"Accept": "application/vnd.github+json",
                     "Content-Type": "application/zip",
                     "User-Agent": USER_AGENT,
                     "Authorization": f"Bearer {self.github_token}"})
        status, raw = self._send(req)
        if status != 201:
            raise ValueError(f"upload assetu {asset_name}: HTTP {status}: {raw[:200]!r}")

    def request_oidc_token(self, audience):
        url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        bearer = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}audience={urllib.parse.quote(audience, safe='')}"
        req = urllib.request.Request(
            full, headers={"Authorization": f"bearer {bearer}",
                           "Accept": "application/json",
                           "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["value"]

    def post_publish(self, token, payload):
        req = urllib.request.Request(
            PUBLISH_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        status, raw = self._send(req)
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, {"error": raw.decode("utf-8", "replace")[:200]}


def main():
    only = os.environ.get("ONLY_PACKAGE", "").strip() or None
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("brak GITHUB_TOKEN w srodowisku", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    report = run(root / "sources.json", root / "state.json",
                 RealOps(token), only_package=only)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    failed = [m for m, r in report.items() if r.get("publish") == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
