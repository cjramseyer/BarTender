import argparse
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bartender.app as app_module


SAMPLE_SOURCE = ROOT / "sample_data" / "demo_bar_data.json"


def load_sample_source() -> dict:
    with open(SAMPLE_SOURCE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_sample_export_assets(output_dir: Path) -> tuple[Path, Path]:
    sample_source = load_sample_source()

    with tempfile.TemporaryDirectory() as tmp_dir:
        temp_dir = Path(tmp_dir)
        app_module.DATA_DIR = temp_dir
        app_module.DATA_FILE = temp_dir / "bartender.json"
        app_module.UPLOADS_DIR = temp_dir / "uploads"
        app_module.UPLOADS_DIR.mkdir(exist_ok=True)

        app_module.save_data(sample_source)
        normalized_data = app_module.load_data()

    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "bartender_sample_export.json"
    zip_path = output_dir / "bartender_sample_export.zip"

    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(app_module._build_export_json_payload(normalized_data), handle, indent=2)
        handle.write("\n")

    with open(zip_path, "wb") as handle:
        handle.write(app_module._build_export_archive(normalized_data))

    return json_path, zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build sample BarTender release assets.")
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "dist"),
        help="Directory where the sample export assets will be written.",
    )
    args = parser.parse_args()

    json_path, zip_path = build_sample_export_assets(Path(args.output_dir))
    print(json_path)
    print(zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())