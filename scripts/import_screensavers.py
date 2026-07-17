from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from screensaver_manager import ScreensaverManager


def parse_classification(filename: str) -> tuple[str, list[str]]:
    if "[不建议-" in filename:
        return "UNSUITABLE", []
    suitability = "CANDIDATE" if "[候选-" in filename else "SUITABLE"
    if "L主副全屏" in filename:
        targets = ["L_MAIN_FULL", "L_SECONDARY_FULL"]
    elif "L主屏半屏" in filename:
        targets = ["L_MAIN_HALF"]
    elif "S猎装GT主屏" in filename:
        targets = ["S_GT_MAIN"]
    elif "S猎装GT副屏" in filename:
        targets = ["S_GT_SECONDARY"]
    else:
        raise ValueError(f"无法从文件名解析适配目标: {filename}")
    return suitability, targets


def main() -> None:
    parser = argparse.ArgumentParser(description="批量导入已分类的屏保资源")
    parser.add_argument("source", type=Path)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    manager = ScreensaverManager(args.repo / "screensavers.json", args.repo / "screensavers")
    files = sorted(path for path in args.source.iterdir() if path.is_file())
    if not files:
        raise SystemExit("源目录没有文件")
    data = manager.load_data()
    if data.get("screensavers"):
        raise SystemExit("screensavers.json 非空，拒绝重复导入")

    tmp_root = Path(tempfile.mkdtemp(prefix="screensaver-import-", dir=args.repo))
    entries: list[dict] = []
    staged_dirs: list[Path] = []
    try:
        for index, source in enumerate(files, start=1):
            suitability, targets = parse_classification(source.name)
            entry, staged_dir = manager.prepare_entry(source, source.name, suitability, targets, tmp_root)
            entries.append(entry)
            staged_dirs.append(staged_dir)
            print(f"[{index}/{len(files)}] {entry['name']} {entry['width']}x{entry['height']}")
        manager.commit_batch(data, entries, staged_dirs)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    main()
