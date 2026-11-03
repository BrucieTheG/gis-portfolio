"""Run the whole ETL pipeline in order, then the data and network QC reports.

Each step replaces its tables, so rerunning is safe. Downloads are cached in data/raw/
(delete a file there to force a fresh copy). 07_network is built from core.roads, so rerun it
after 02_osm.

    .venv\\Scripts\\python etl\\run_all.py            # everything
    .venv\\Scripts\\python etl\\run_all.py 02 07      # only some steps (QC still runs)
"""
import subprocess
import sys
import time
from pathlib import Path

ETL = Path(__file__).resolve().parent
STEPS = ["01_boundaries", "02_osm", "03_buildings", "04_gtfs", "05_flood", "06_hydro",
         "07_network"]
REPORTS = {"qc_report": "docs/data-qc.md", "network_qc": "docs/network-qc.md"}


def main(argv: list[str]) -> int:
    steps = [s for s in STEPS if not argv or s[:2] in argv]
    t0 = time.time()
    for step in steps:
        print(f"\n=== {step} ===", flush=True)
        if subprocess.run([sys.executable, str(ETL / f"{step}.py")]).returncode != 0:
            print(f"\n{step} failed; stopping.", flush=True)
            return 1
    failed = []
    for report, doc in REPORTS.items():
        print(f"\n=== {report} ===", flush=True)
        if subprocess.run([sys.executable, str(ETL / f"{report}.py")]).returncode != 0:
            failed.append(doc)
    print(f"\nPipeline finished in {(time.time() - t0) / 60:.1f} min "
          f"({'QC passed' if not failed else 'QC FAILED - see ' + ', '.join(failed)}).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
