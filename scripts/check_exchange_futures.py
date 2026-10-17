"""Daily advisory comparison using already collected vendor quotes."""
import argparse
import json
from china_commodities.futures_crosscheck import run_crosscheck

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--raw-dir", default="tmp/exchange-crosscheck/raw")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()
    print(json.dumps(run_crosscheck(args.date, data_dir=args.data_dir, raw_dir=args.raw_dir, output_dir=args.output_dir), ensure_ascii=False, indent=2))
