"""Budowa deterministycznego wariantu .mpackage z polem created.

Wariant rozni sie od asseta zrodlowego wylacznie jedna linia w config.lua
(`created = "..."`, wymog packages.mudlet.org). Zip skladany jest na sztywno:
STORED, timestampy 1980-01-01, wpisy sortowane, stale atrybuty — kazda
przebudowa z tego samego asseta daje identyczny SHA-256.
"""

import io
import re
import zipfile

REQUIRED_CONFIG_FIELDS = ("mpackage", "title", "version", "created", "author", "description")

FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

CREATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

_FIELD_RES = {field: re.compile(rf"^\s*{field}\s*=", re.MULTILINE)
              for field in REQUIRED_CONFIG_FIELDS}

_EXTERNAL_ATTR = 0o600 << 16


def missing_fields(config_text):
    """Lista brakujacych wymaganych pol (lustro walidacji packages.mudlet.org)."""
    return [field for field in REQUIRED_CONFIG_FIELDS
            if not _FIELD_RES[field].search(config_text)]


def add_created(config_text, created):
    """Dokleja linie created na koncu config.lua; odmawia, gdy pole juz jest."""
    if not CREATED_RE.match(created):
        raise ValueError(f"zly format created: {created!r}")
    if _FIELD_RES["created"].search(config_text):
        raise ValueError("config.lua juz zawiera pole created")
    if not config_text.endswith("\n"):
        config_text += "\n"
    return config_text + f'created = "{created}"\n'


def variant_tag(mpackage, version):
    return f"{mpackage}-v{version}"


def variant_asset_name(mpackage):
    return f"{mpackage}.mpackage"


def read_members(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return {name: z.read(name) for name in z.namelist()}


def build_variant(asset_bytes, created):
    """Wariant .mpackage: config.lua z created, reszta bajtowo, zip deterministyczny."""
    members = read_members(asset_bytes)
    if "config.lua" not in members:
        raise ValueError("brak config.lua w pakiecie")
    config = members["config.lua"].decode("utf-8")
    members["config.lua"] = add_created(config, created).encode("utf-8")
    missing = missing_fields(members["config.lua"].decode("utf-8"))
    if missing:
        raise ValueError(f"config.lua bez wymaganych pol: {', '.join(missing)}")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as z:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = _EXTERNAL_ATTR
            info.create_system = 3
            z.writestr(info, members[name])
    return out.getvalue()
