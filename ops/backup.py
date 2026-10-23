"""SQLite-consistent manual snapshot backup for the ONE Blotter database."""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile

def backup_database(database: Path, output_directory: Path) -> Path:
    """Make a consistent backup without deleting old snapshots."""
    database=Path(database)
    output_directory=Path(output_directory)
    if not database.is_file():
        raise FileNotFoundError("source Blotter database not found")
    if not output_directory.is_dir():
        raise FileNotFoundError("backup directory must pre-exist with protected permissions")
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target=output_directory/("blotter-"+stamp+".sqlite3")
    fd, temporary=tempfile.mkstemp(dir=str(output_directory),suffix=".tmp")
    os.close(fd)
    try:
        with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                status=dst.execute("PRAGMA quick_check").fetchone()[0]
                if status != "ok":
                    raise RuntimeError("backup failed SQLite quick_check")
        if target.exists():raise FileExistsError("backup file already exists")
        os.replace(temporary,target)
        return target
    finally:
        if os.path.exists(temporary):os.unlink(temporary)

def main():
    parser=argparse.ArgumentParser(description="Create protected consistent Blotter snapshot")
    parser.add_argument("--db",required=True)
    parser.add_argument("--output-dir",required=True)
    args=parser.parse_args()
    path=backup_database(Path(args.db),Path(args.output_dir))
    print(json.dumps({"ok":True,"file":str(path),"size_bytes":path.stat().st_size}))
if __name__=="__main__":main()
