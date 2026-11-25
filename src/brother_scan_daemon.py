#!/usr/bin/env python3
"""
brother-scan-to-paperless: A lightweight daemon that listens for Brother
scanner button presses and saves scans directly to a Paperless-ngx
consume folder using scanimage.

Works reliably in LXC containers where the official brscan-skey tool
often fails due to forking/daemon issues.

Requires: brscan4 driver, sane-utils, snmp
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime

__version__ = "0.0.3"

DEFAULT_CONFIG_PATHS = [
    "/etc/brother-scan-to-paperless/config.json",
    os.path.expanduser("~/.config/brother-scan-to-paperless/config.json"),
]

DEFAULT_CONFIG = {
    "printer_ip": "",
    "host_ip": "",
    "listen_port": 54925,
    "consume_dir": "",
    "scanner_device": "brother4:net1;dev0",
    "resolution": 300,
    "source": "FB",
    "size": "A4",
    "format": "tiff",
    "register_interval": 300,
    "scan_timeout": 120,
    "batch_idle_seconds": 45,
    "batch_max_wait_seconds": 300,
    "batch_max_files": 10,
    "dedupe_seconds": 900,
    "merge_batch": False,
    "interleave_batch": True,
    "collate_reverse_second": True,
    "display_name": "Paperless",
    "log_file": "/var/log/brother-scan-to-paperless.log",
}


def log(msg: str, log_file: str | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    if log_file:
        with open(log_file, "a") as f:
            f.write(line + "\n")


def detect_host_ip() -> str | None:
    """Detect the host IP by opening a UDP socket to a public DNS."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def detect_scanner_device() -> str | None:
    """Try to detect the scanner device name using brsaneconfig4."""
    try:
        result = subprocess.run(
            ["brsaneconfig4", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("*") and "brother" in line.lower():
                # Extract device name from brsaneconfig4 output
                return "brother4:net1;dev0"
        return None
    except Exception:
        return None


def register_profiles(
    printer_ip: str,
    host_ip: str,
    listen_port: int,
    display_name: str,
    log_file: str | None,
) -> bool:
    """Register scan profiles on the printer via SNMP."""
    oid = ".1.3.6.1.4.1.2435.2.3.9.2.11.1.1.0"
    community = "internal"

    functions = [
        ("IMAGE", 1),
        ("EMAIL", 2),
        ("OCR", 3),
        ("FILE", 5),
    ]

    cmd = ["snmpset", "-v1", "-c", community, printer_ip]
    for func, appnum in functions:
        profile = (
            f'TYPE=BR;BUTTON=SCAN;USER="{display_name}";'
            f"FUNC={func};HOST={host_ip}:{listen_port};"
            f"APPNUM={appnum};DURATION=360;BRID=;"
        )
        cmd.extend([oid, "s", profile])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            log("Registered scan profiles on printer", log_file)
            return True
        else:
            log(f"SNMP registration failed: {result.stderr.strip()}", log_file)
            return False
    except Exception as e:
        log(f"SNMP registration error: {e}", log_file)
        return False


SOURCE_ALIASES = {
    "FB": "FlatBed",
    "FLATBED": "FlatBed",
    "ADF": "Automatic Document Feeder(left aligned)",
}


def tiffcp_merge(paths: list[str], destination: str, timeout: int = 300) -> None:
    """Combine TIFFs into one multi-page TIFF, failing on an empty result."""
    result = subprocess.run(
        ["tiffcp", "-c", "lzw", *paths, destination],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if (
        result.returncode != 0
        or not os.path.exists(destination)
        or os.path.getsize(destination) == 0
    ):
        raise RuntimeError(
            f"tiffcp exit={result.returncode} {result.stderr.strip()}",
        )


def interleave_tiffs(
    paths: list[str],
    destination: str,
    reverse_second: bool = True,
) -> None:
    """Interleave the pages of two TIFF jobs into one multi-page TIFF.

    The first job is treated as the front stack and the second as the back
    stack. When ``reverse_second`` is true the back stack is reversed, which
    matches feeding the flipped stack through a simplex ADF.
    """
    if len(paths) != 2:
        raise ValueError("interleave requires exactly two jobs")

    workdir = tempfile.mkdtemp(prefix="brother-scan-")
    try:
        page_sets: list[list[str]] = []
        for index, path in enumerate(paths):
            prefix = os.path.join(workdir, f"job{index}_")
            result = subprocess.run(
                ["tiffsplit", str(path), prefix],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"tiffsplit exit={result.returncode} "
                    f"{result.stderr.strip()}",
                )
            pages = sorted(glob.glob(prefix + "*"))
            if not pages:
                raise RuntimeError(f"tiffsplit produced no pages for {path}")
            page_sets.append(pages)

        front, back = page_sets
        ordered: list[str] = []
        for index in range(max(len(front), len(back))):
            if index < len(front):
                ordered.append(front[index])
            back_index = len(back) - 1 - index if reverse_second else index
            if 0 <= back_index < len(back):
                ordered.append(back[back_index])

        tiffcp_merge(ordered, destination)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)



def scan_to_file(
    config: dict,
    batch_id: str,
    seq: int,
) -> tuple[str, str] | None:
    """Run scanimage into a hidden temp file.

    Returns ``(temporary_path, final_path)`` when the scan succeeded, leaving
    publication to the caller. The temporary name is hidden so Paperless
    ignores it until the batch is complete.

    On the ADF, ``scanimage`` is run in batch mode and the page files are
    combined into one multi-page TIFF before returning.
    """
    ext = config["format"]
    outfile = os.path.join(
        config["consume_dir"],
        f"scan_{batch_id}_{seq:02d}.{ext}",
    )
    tmpfile = os.path.join(
        config["consume_dir"],
        f".scan_{batch_id}_{seq:02d}.{ext}.part",
    )
    page_pattern = os.path.join(
        config["consume_dir"],
        f".scan_{batch_id}_{seq:02d}_page_%03d.{ext}.part",
    )
    log_file = config.get("log_file")

    source = str(config.get("source", "ADF"))
    source = SOURCE_ALIASES.get(source.upper(), source)
    use_adf_batch = "document feeder" in source.lower() and ext.lower() in (
        "tif",
        "tiff",
    )

    log(f"Starting scan -> {outfile} (source={source})", log_file)

    def cleanup_tmp() -> None:
        if os.path.exists(tmpfile):
            os.remove(tmpfile)

    def cleanup_pages() -> None:
        for path in glob.glob(page_pattern.replace("%03d", "*")):
            try:
                os.remove(path)
            except OSError:
                pass

    def run_retry(cmd: list[str], has_pages: bool = False):
        """Run scanimage, retrying the device-open race once or twice."""
        last = None
        for attempt in range(1, 4):
            last = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config["scan_timeout"],
            )
            if last.returncode == 0:
                return last
            if has_pages and glob.glob(page_pattern.replace("%03d", "*")):
                return last
            stderr = (last.stderr or "").lower()
            if "invalid argument" not in stderr or attempt == 3:
                return last
            log(
                f"Scan retry {attempt}/2 after: {last.stderr.strip()}",
                log_file,
            )
            time.sleep(3)
        return last

    try:
        base_cmd = [
            "scanimage",
            f"--device-name={config['scanner_device']}",
            f"--resolution={config['resolution']}",
            f"--format={config['format']}",
            f"--source={source}",
        ]

        if use_adf_batch:
            cleanup_pages()
            cmd = base_cmd + [f"--batch={page_pattern}"]
            result = run_retry(cmd, has_pages=True)
            pages = sorted(glob.glob(page_pattern.replace("%03d", "*")))
            if not pages:
                log(
                    f"Scan failed: exit={getattr(result, 'returncode', '?')} "
                    f"stderr={(getattr(result, 'stderr', '') or '').strip()}",
                    log_file,
                )
                cleanup_tmp()
                return None
            if result.returncode != 0:
                log(
                    f"Scan returned exit={result.returncode} after "
                    f"{len(pages)} page(s); combining partial batch",
                    log_file,
                )
            try:
                tiffcp_merge(pages, tmpfile, timeout=120)
            except Exception as e:
                log(f"tiffcp failed: {e}", log_file)
                cleanup_tmp()
                cleanup_pages()
                return None
            size = os.path.getsize(tmpfile)
            log(
                f"Scanned {len(pages)} page(s) into {tmpfile} ({size:,} bytes)",
                log_file,
            )
            cleanup_pages()
            return tmpfile, outfile

        # Flatbed or non-TIFF output: one page.
        cmd = base_cmd + [f"--output-file={tmpfile}"]
        result = run_retry(cmd)
        if (
            result.returncode == 0
            and os.path.exists(tmpfile)
            and os.path.getsize(tmpfile) > 0
        ):
            return tmpfile, outfile

        log(
            f"Scan failed: exit={getattr(result, 'returncode', '?')} "
            f"stderr={(getattr(result, 'stderr', '') or '').strip()}",
            log_file,
        )
        cleanup_tmp()
        return None

    except subprocess.TimeoutExpired:
        log(f"Scan timed out after {config['scan_timeout']}s", log_file)
        cleanup_tmp()
        cleanup_pages()
        return None
    except FileNotFoundError as e:
        log(f"Scan command missing: {e}", log_file)
        cleanup_tmp()
        cleanup_pages()
        return None
    except Exception as e:
        log(f"Scan error: {e}", log_file)
        cleanup_tmp()
        cleanup_pages()
        return None


def do_scan(config: dict) -> bool:
    """Run one scan and publish it immediately (used by --test)."""
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = scan_to_file(config, batch_id, 1)
    if result is None:
        return False

    tmpfile, outfile = result
    # Atomic within the consume filesystem: Paperless either sees the
    # temporary file (ignored) or the complete final image.
    os.replace(tmpfile, outfile)
    size = os.path.getsize(outfile)
    log(f"Scan complete: {outfile} ({size:,} bytes)", config.get("log_file"))
    return True


def load_config(config_path: str | None = None) -> dict:
    """Load config from file, falling back to defaults."""
    config = DEFAULT_CONFIG.copy()

    paths_to_try = [config_path] if config_path else DEFAULT_CONFIG_PATHS
    for path in paths_to_try:
        if path and os.path.exists(path):
            with open(path) as f:
                user_config = json.load(f)
            config.update(user_config)
            break

    return config


def validate_config(config: dict) -> list[str]:
    """Validate config and return list of error messages."""
    errors = []

    if not config.get("printer_ip"):
        errors.append("printer_ip is required")
    if not config.get("host_ip"):
        detected = detect_host_ip()
        if detected:
            config["host_ip"] = detected
        else:
            errors.append("host_ip is required (auto-detection failed)")
    if not config.get("consume_dir"):
        errors.append("consume_dir is required")
    elif not os.path.isdir(config["consume_dir"]):
        errors.append(f"consume_dir does not exist: {config['consume_dir']}")

    return errors


def check_dependencies() -> list[str]:
    """Check that required commands are available."""
    missing = []
    for cmd in ["scanimage", "snmpset", "brsaneconfig4"]:
        if not any(
            os.access(os.path.join(d, cmd), os.X_OK)
            for d in os.environ.get("PATH", "").split(":")
        ):
            missing.append(cmd)
    return missing


def run_daemon(config: dict) -> None:
    """Main daemon loop."""
    log_file = config.get("log_file")

    pending: list[dict] = []
    batch_id: str | None = None
    first_scan_at: float | None = None
    last_scan_at: float | None = None
    seen_events: dict[tuple[str, str], float] = {}

    def publish_pending() -> None:
        """Publish the held scans, merged into one TIFF when configured."""
        nonlocal pending, batch_id, first_scan_at, last_scan_at
        if not pending:
            return

        ext = str(config.get("format", "tiff"))
        merge_batch = bool(config.get("merge_batch", False)) and ext.lower() in (
            "tif",
            "tiff",
        )
        if merge_batch:
            merged_tmp = os.path.join(
                config["consume_dir"],
                f".scan_{batch_id}.{ext}.part",
            )
            merged_final = os.path.join(
                config["consume_dir"],
                f"scan_{batch_id}.{ext}",
            )
            published = False
            try:
                if len(pending) == 1:
                    os.replace(pending[0]["tmp"], merged_tmp)
                elif len(pending) == 2 and bool(
                    config.get("interleave_batch", True),
                ):
                    try:
                        interleave_tiffs(
                            [item["tmp"] for item in pending],
                            merged_tmp,
                            reverse_second=bool(
                                config.get("collate_reverse_second", True),
                            ),
                        )
                    except Exception as e:
                        log(
                            f"Batch {batch_id}: interleave failed ({e}); "
                            f"concatenating",
                            log_file,
                        )
                        tiffcp_merge(
                            [item["tmp"] for item in pending],
                            merged_tmp,
                        )
                else:
                    tiffcp_merge(
                        [item["tmp"] for item in pending],
                        merged_tmp,
                    )
                os.replace(merged_tmp, merged_final)
                size = os.path.getsize(merged_final)
                log(
                    f"Batch {batch_id}: merged {len(pending)} scan(s) into "
                    f"{merged_final} ({size:,} bytes)",
                    log_file,
                )
                published = True
            except Exception as e:
                log(
                    f"Batch {batch_id}: merge failed ({e}); publishing separately",
                    log_file,
                )
                try:
                    if os.path.exists(merged_tmp):
                        os.remove(merged_tmp)
                except OSError:
                    pass

            if published:
                for item in pending:
                    try:
                        if os.path.exists(item["tmp"]):
                            os.remove(item["tmp"])
                    except OSError:
                        pass
                pending = []
                batch_id = None
                first_scan_at = None
                last_scan_at = None
                return

        for item in pending:
            try:
                os.replace(item["tmp"], item["final"])
                size = os.path.getsize(item["final"])
                log(
                    f"Batch {item['batch']}: published {item['final']} ({size:,} bytes)",
                    log_file,
                )
            except Exception as e:
                log(
                    f"Batch {item['batch']}: failed to publish {item['final']}: {e}",
                    log_file,
                )
                try:
                    if os.path.exists(item["tmp"]):
                        os.remove(item["tmp"])
                except OSError:
                    pass
        pending = []
        batch_id = None
        first_scan_at = None
        last_scan_at = None

    def batch_expired(now: float) -> bool:
        if not pending:
            return False
        idle = max(0, int(config.get("batch_idle_seconds", 45)))
        max_wait = max(0, int(config.get("batch_max_wait_seconds", 300)))
        if idle and last_scan_at is not None and now - last_scan_at >= idle:
            return True
        if max_wait and first_scan_at is not None and now - first_scan_at >= max_wait:
            return True
        return False

    def cleanup_orphans() -> None:
        patterns = (".scan_*.part", ".scan_*_page_*")
        for pattern in patterns:
            for path in glob.glob(os.path.join(config["consume_dir"], pattern)):
                try:
                    os.remove(path)
                    log(f"Removed orphaned temporary scan {path}", log_file)
                except OSError as e:
                    log(f"Could not remove orphaned scan {path}: {e}", log_file)

    log(f"brother-scan-to-paperless {__version__} daemon starting", log_file)
    log(f"  Printer:    {config['printer_ip']}", log_file)
    log(f"  Host:       {config['host_ip']}", log_file)
    log(f"  Consume:    {config['consume_dir']}", log_file)
    log(f"  Device:     {config['scanner_device']}", log_file)
    log(f"  Resolution: {config['resolution']} dpi", log_file)
    log(f"  Format:     {config['format']}", log_file)
    log(f"  Source:     {config.get('source', 'ADF')}", log_file)
    log(
        f"  Batch:      {config.get('batch_idle_seconds', 45)}s idle, "
        f"{config.get('batch_max_wait_seconds', 300)}s max, "
        f"{config.get('batch_max_files', 10)} files, "
        f"merge={bool(config.get('merge_batch', False))}",
        log_file,
    )

    cleanup_orphans()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config["listen_port"]))
    sock.settimeout(30)

    def shutdown(signum, frame):
        log("Shutting down", log_file)
        publish_pending()
        sock.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    register_profiles(
        config["printer_ip"],
        config["host_ip"],
        config["listen_port"],
        config["display_name"],
        log_file,
    )
    last_register = time.time()

    log(f"Listening on UDP port {config['listen_port']}", log_file)

    while True:
        if batch_expired(time.time()):
            publish_pending()

        if time.time() - last_register > config["register_interval"]:
            register_profiles(
                config["printer_ip"],
                config["host_ip"],
                config["listen_port"],
                config["display_name"],
                log_file,
            )
            last_register = time.time()

        timeout = 30.0
        if pending:
            now = time.time()
            idle = max(0, int(config.get("batch_idle_seconds", 45)))
            max_wait = max(0, int(config.get("batch_max_wait_seconds", 300)))
            deadlines = []
            if idle and last_scan_at is not None:
                deadlines.append(last_scan_at + idle - now)
            if max_wait and first_scan_at is not None:
                deadlines.append(first_scan_at + max_wait - now)
            if deadlines:
                timeout = max(0.5, min(30.0, min(deadlines)))
        sock.settimeout(timeout)

        try:
            data, addr = sock.recvfrom(1024)
            msg = data.decode("utf-8", errors="replace")
            log(f"Received from {addr}: {msg[:120]}", log_file)

            regid_match = re.search(r"REGID=([^;\s]+)", msg)
            seq_match = re.search(r"SEQ=([^;\s]+)", msg)
            if regid_match and seq_match:
                event_key = (regid_match.group(1), seq_match.group(1))
                now = time.time()
                dedupe = max(0, int(config.get("dedupe_seconds", 900)))
                for key, seen_at in list(seen_events.items()):
                    if now - seen_at > dedupe:
                        del seen_events[key]
                if event_key in seen_events:
                    log(
                        f"Ignoring duplicate scan event "
                        f"REGID={event_key[0]} SEQ={event_key[1]}",
                        log_file,
                    )
                    continue
                seen_events[event_key] = now

            if "BUTTON=SCAN" in msg:
                now = time.time()
                if batch_id is None:
                    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    first_scan_at = now

                seq = len(pending) + 1
                result = scan_to_file(config, batch_id, seq)
                if result is not None:
                    tmpfile, outfile = result
                    pending.append(
                        {"batch": batch_id, "tmp": tmpfile, "final": outfile},
                    )
                    last_scan_at = time.time()
                    log(
                        f"Batch {batch_id}: {len(pending)} scan(s) held; "
                        f"publishing after "
                        f"{config.get('batch_idle_seconds', 45)}s idle",
                        log_file,
                    )
                    max_files = max(0, int(config.get("batch_max_files", 10)))
                    if max_files and len(pending) >= max_files:
                        log(
                            f"Batch {batch_id}: reached {max_files} files",
                            log_file,
                        )
                        publish_pending()
                elif not pending:
                    batch_id = None
                    first_scan_at = None
                    last_scan_at = None

        except socket.timeout:
            if batch_expired(time.time()):
                publish_pending()
            continue
        except Exception as e:
            log(f"Error: {e}", log_file)
            time.sleep(1)


def cmd_setup(args: argparse.Namespace) -> None:
    """Interactive setup wizard."""
    print("brother-scan-to-paperless setup\n")

    config = DEFAULT_CONFIG.copy()

    detected_ip = detect_host_ip()

    config["printer_ip"] = input("Printer IP address: ").strip()

    default_host = detected_ip or ""
    host = input(f"This machine's IP address [{default_host}]: ").strip()
    config["host_ip"] = host or default_host

    config["consume_dir"] = input("Paperless-ngx consume directory path: ").strip()

    resolution = input(f"Scan resolution in DPI [{config['resolution']}]: ").strip()
    if resolution:
        config["resolution"] = int(resolution)

    name = input(f"Display name on printer [{config['display_name']}]: ").strip()
    if name:
        config["display_name"] = name

    errors = validate_config(config)
    if errors:
        print("\nConfiguration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    config_dir = "/etc/brother-scan-to-paperless"
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.json")

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nConfig saved to {config_path}")
    print("\nRegister scanner with brscan4:")
    print(
        f"  brsaneconfig4 -a name=BROTHER model=DCP-L2540DW ip={config['printer_ip']}"
    )
    print("\nStart the daemon:")
    print("  systemctl enable --now brother-scan-to-paperless")


def cmd_test(args: argparse.Namespace) -> None:
    """Test scan without the daemon."""
    config = load_config(args.config)
    if args.printer_ip:
        config["printer_ip"] = args.printer_ip
    if args.consume_dir:
        config["consume_dir"] = args.consume_dir

    errors = validate_config(config)
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("Testing scan...")
    success = do_scan(config)
    if success:
        print("Test scan succeeded!")
    else:
        print("Test scan failed. Check logs.")
        sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    """Run the daemon."""
    config = load_config(args.config)

    # CLI overrides
    if args.printer_ip:
        config["printer_ip"] = args.printer_ip
    if args.host_ip:
        config["host_ip"] = args.host_ip
    if args.consume_dir:
        config["consume_dir"] = args.consume_dir
    if args.resolution:
        config["resolution"] = args.resolution
    if args.display_name:
        config["display_name"] = args.display_name

    missing = check_dependencies()
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Install with: apt install sane-utils snmp")
        if "brsaneconfig4" in missing:
            print(
                "Install brscan4 from: "
                "https://support.brother.com/g/b/downloadlist.aspx"
                "?c=us&lang=en&prod=dcpl2540dw_us_as&os=128"
            )
        sys.exit(1)

    errors = validate_config(config)
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        print("\nRun 'brother-scan-to-paperless setup' to configure.")
        sys.exit(1)

    run_daemon(config)


def main():
    parser = argparse.ArgumentParser(
        prog="brother-scan-to-paperless",
        description=(
            "Lightweight daemon that listens for Brother scanner button "
            "presses and saves scans to a Paperless-ngx consume folder."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command")

    # run
    run_parser = subparsers.add_parser("run", help="Run the scan daemon")
    run_parser.add_argument("-c", "--config", help="Path to config file")
    run_parser.add_argument("--printer-ip", help="Printer IP address")
    run_parser.add_argument("--host-ip", help="This machine's IP address")
    run_parser.add_argument("--consume-dir", help="Paperless consume directory")
    run_parser.add_argument("--resolution", type=int, help="Scan resolution (DPI)")
    run_parser.add_argument("--display-name", help="Name shown on printer display")

    # setup
    subparsers.add_parser("setup", help="Interactive setup wizard")

    # test
    test_parser = subparsers.add_parser("test", help="Test a single scan")
    test_parser.add_argument("-c", "--config", help="Path to config file")
    test_parser.add_argument("--printer-ip", help="Printer IP address")
    test_parser.add_argument("--consume-dir", help="Paperless consume directory")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "test":
        cmd_test(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
