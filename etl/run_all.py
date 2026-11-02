"""Run the whole ETL pipeline in order, then the QC report.

Each step replaces its tables, so rerunning is safe. Downloads are cached in data/raw/
(delete a file there to force a fresh copy).

    .venv\\Scripts\\python etl\\run_all.py            # everything
    .venv\\Scripts\\python etl\\run_all.py 03 05      # only some steps (QC still runs)
"""
import subprocess
import sys
import time
from pathlib import Path

ETL = Path(__file__).resolve().parent
STEPS = ["01_boundaries", "02_osm", "03_buildings", "04_gtfs", "05_flood", "06_hydro"]


def main(argv: list[str]) -> int:
    steps = [s for s in STEPS if not argv or s[:2] in argv]
    t0 = time.time()
    for step in steps:
        print(f"\n=== {step} ===", flush=True)
        if subprocess.run([sys.executable, str(ETL / f"{step}.py")]).returncode != 0:
            print(f"\n{step} failed; stopping.", flush=True)
            return 1
    print("\n=== qc_report ===", flush=True)
    rc = subprocess.run([sys.executable, str(ETL / "qc_report.py")]).returncode
    print(f"\nPipeline finished in {(time.time() - t0) / 60:.1f} min "
          f"({'QC passed' if rc == 0 else 'QC FAILED - see docs/data-qc.md'}).")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
