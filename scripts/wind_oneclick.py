"""One foreground run of the commodities Wind collector; no stock actions."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
SUCCESS = {"published", "up_to_date"}


class CollectorError(RuntimeError):
    def __init__(self, root, code, payload):
        super().__init__(f"{root.name}: exit {code}; {payload or 'see log'}")
        self.payload = dict(payload, exit_code=code)


def failure_result(exc, state_key="state"):
    if isinstance(exc, CollectorError) and ("state" in exc.payload or "phase" in exc.payload):
        return exc.payload
    return {state_key: "launcher_error", "error": str(exc)}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


@contextmanager
def single_run():
    # OS lock is released after an abnormal exit; no stale PID lock to remove.
    with (BASE / "launcher.lock").open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another launcher is running.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another launcher is running.") from exc
        yield


def repositories():
    path = BASE / "launcher.json"
    settings = read_json(path) if path.exists() else {}
    result = {}
    for key, name in (("commodities", "China-Commodities-Engine"),):
        value = settings.get(key)
        candidate = BASE.parent / name
        if not value and candidate.is_dir():
            value = str(candidate)
        if not value or not Path(value).is_dir():
            value = input(f"{name} repository full path: ").strip().strip('"')
        root = Path(value).expanduser().resolve()
        if not (root / "src").is_dir():
            raise RuntimeError(f"Invalid repository: {root}")
        result[key] = root
    save_json(path, {key: str(root) for key, root in result.items()})
    return result


def python_for(root):
    path = root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not path.is_file():
        raise RuntimeError(f"Missing repository Python: {path}. Complete the repository Mac SDK setup first.")
    return str(path)


def invoke(root, args, log):
    command = [python_for(root), *args]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env["PYTHONPATH"] = str(root / "src")
    print(f"\n[{root.name}] {' '.join(args)}", flush=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"\nCOMMAND {command!r}\n")
        handle.flush()
        with subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") as process:
            output = []
            try:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    handle.write(line)
                    handle.flush()
                    output.append(line)
                code = process.wait()
            except BaseException:
                # Let the repository supervisor clean up its own SDK worker.
                if process.poll() is None:
                    print("Waiting for the collector to exit. Do not start another copy.", flush=True)
                    process.wait()
                raise
    raw = "".join(output)
    decoder = json.JSONDecoder()
    payload = {}
    for pos, char in enumerate(raw):
        if char == "{":
            try:
                obj, _ = decoder.raw_decode(raw[pos:])
                if isinstance(obj, dict) and ("state" in obj or "phase" in obj):
                    payload = obj
                    break
            except ValueError:
                pass
    if code:
        raise CollectorError(root, code, payload)
    return payload


def prepare(roots, run_dir, *, parallel=True, request_interval=0):
    if isinstance(request_interval, bool) or not isinstance(request_interval, (int, float)) or not math.isfinite(request_interval) or request_interval < 0:
        raise ValueError("Request interval must be finite and nonnegative")
    configs = {}
    for key in ('commodities',):
        root = roots[key]
        python_for(root)
        if not (root / "scripts/run_wind_daily.py").is_file():
            raise RuntimeError("Commodities daily entry point missing; update the repository first.")
        source = root / "tmp/wind-daily/config.json"
        cfg = read_json(source) if source.exists() else {}
        # Only this invocation is enabled; do not activate installed background tasks.
        cfg["enabled"] = True
        cfg["allow_dashboard"] = True
        cfg["request_interval_seconds"] = request_interval
        cfg["allow_parallel_collectors"] = parallel
        cfg["max_days_per_run"] = min(1, cfg.get("max_days_per_run", 5))
        destination = run_dir / f"{key}-config.json"
        save_json(destination, cfg)
        configs[key] = destination
    return configs


def confirm_login():
    print("\n请确认本机官方 Wind 已登录，API 可用。")
    print("Trading Desk 可以保持运行；请勿在另一台电脑使用同账号同时采集。")
    print("按上方模式运行；保留各自防重复启动和数据校验。")
    if input("输入 YES 开始本轮取数：").strip().upper() != "YES":
        raise RuntimeError("Cancelled; no login signal sent.")
    return time.monotonic()


def collect_commodities(roots, configs, run_dir, call=invoke):
    results = {}
    root = roots["commodities"]
    log = run_dir / "commodities.log"
    try:
        call(root, ["scripts/run_wind_daily.py", "--signal-login"], log)
        result = call(root, ["scripts/run_wind_daily.py", "--once", "--config", str(configs["commodities"])], log)
        results["commodities"] = result or {"phase": "unknown", "error": "No structured result"}
    except Exception as exc:
        results["commodities"] = failure_result(exc, "phase")
    finally:
        # Avoid leaving an unused launcher signal for a separate watcher.
        (root / "tmp/wind-daily/login-ready.json").unlink(missing_ok=True)
    return results["commodities"]


def collect(roots, configs, run_dir, confirmed_at, call=invoke, confirm=confirm_login, *, parallel=True):
    if time.monotonic() - confirmed_at >= 900:
        confirm()
    results = {"commodities": collect_commodities(roots, configs, run_dir, call)}
    save_json(run_dir / "summary.json", results)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Offline preflight only; no login or collection")
    parser.add_argument("--serial", action="store_true", help="Use the optional cross-engine lock for the commodity worker")
    parser.add_argument("--request-interval", type=float, default=0, help="Extra idle seconds after each request (default: 0)")
    args = parser.parse_args()
    if not math.isfinite(args.request_interval) or args.request_interval < 0:
        raise ValueError("Request interval must be finite and nonnegative")
    with single_run():
        roots = repositories()
        run_dir = BASE / "logs" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        run_dir.mkdir(parents=True)
        print(f"Logs: {run_dir}", flush=True)
        configs = prepare(roots, run_dir, parallel=not args.serial, request_interval=args.request_interval)
        if args.check:
            for key, root in roots.items():
                invoke(root, ["scripts/configure_wind.py", "--check"], run_dir / f"{key}.log")
            invoke(roots["commodities"], ["scripts/run_wind_daily.py", "--plan", "--config", str(configs["commodities"])], run_dir / "commodities.log")
            print("Offline preflight passed. No Wind connection was tested.")
            return 0
        git_check = subprocess.run(["git", "-C", str(roots["commodities"]), "status", "--porcelain"],
                                   capture_output=True, text=True)
        if git_check.returncode:
            raise RuntimeError("Commodity Git preflight failed; inspect Git configuration before collecting.")
        if git_check.stdout.strip():
            raise RuntimeError("商品仓库有未提交修改，现有发布器会阻止运行。请先审核、提交并同步本次代码，再启动；启动器不会自动提交代码。")
        print(f"仅商品取数；额外间隔：{args.request_interval:g} 秒。")
        print("仅商品仓库校验后自动提交推送；不读取或操作股票仓库。")
        confirmed_at = confirm_login()
        results = collect(roots, configs, run_dir, confirmed_at, parallel=not args.serial)
        print("\n=== RESULTS ===")
        for key, result in results.items():
            print(f"{key}: {result.get('state', result.get('phase', 'unknown'))}")
            for detail in ("error", "next_retry_at", "retry_after", "cleanup_error"):
                if result.get(detail):
                    print(f"  {detail}: {result[detail]}")
            if result.get("git"):
                print("  Git: " + json.dumps(result["git"], ensure_ascii=False))
        print(f"Details: {run_dir / 'summary.json'}")
        complete = all(r.get("state", r.get("phase")) in SUCCESS for r in results.values())
        print("Commodity batch completed." if complete else "Commodity task incomplete. Read the status/logs before retrying.")
        return 0 if complete else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Interrupted. Check repository status before restarting.", file=sys.stderr)
        raise SystemExit(130)
