from __future__ import annotations

import sys
import tempfile
from pathlib import Path


OPS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS_ROOT))

from xflow.io import canonical_path, write_text_lf


def test_canonical_path_collapses_equivalent_absolute_spellings(tmp_path: Path) -> None:
    alias = Path(str(tmp_path).replace("/private/var/", "/var/"))
    assert canonical_path(alias) == canonical_path(tmp_path)


def test_write_text_lf_uses_utf8_and_lf(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "record.txt"
    write_text_lf(target, "第一行\n第二行\n")
    assert target.read_bytes() == "第一行\n第二行\n".encode("utf-8")


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        test_canonical_path_collapses_equivalent_absolute_spellings(tmp_path)
        test_write_text_lf_uses_utf8_and_lf(tmp_path)
    print("portable runtime ok")


if __name__ == "__main__":
    main()
