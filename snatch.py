#!/usr/bin/env python3
"""oracle-snatch-windows: keep asking OCI for a free-tier A1.Flex (ARM)
instance until capacity appears, then exit.

Only the official OCI API is used (the `oci` Python SDK). Nothing here
touches the console, fingerprints, or rate limits beyond backing off.

Usage:
    python snatch.py --check          # validate local config without calling OCI
    python snatch.py --dry-run        # validate config, list ADs, resolve image
    python snatch.py                  # start snatching (loops until success)
    python snatch.py --once           # single round across all ADs, then exit

Requires: pip install -r requirements.txt
Credentials come from the standard OCI config file (~/.oci/config, or the
path in OCI_CONFIG_FILE). Never commit that file or your private key.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

try:
    import oci
    from oci.core import ComputeClient
    from oci.exceptions import ServiceError, ConfigFileNotFound
    from oci.identity import IdentityClient
except ImportError:
    sys.exit("The 'oci' package is not installed. Run: pip install -r requirements.txt")

SHAPE = "VM.Standard.A1.Flex"
HERE = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(HERE, "snatched.json")


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file(path):
    """Minimal KEY=VALUE loader so the script needs no extra dependencies."""
    cfg = {}
    if not path or not os.path.exists(path):
        return cfg
    with open(path, encoding="utf-8-sig") as f:  # utf-8-sig tolerates Windows BOM
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def get_setting(file_cfg, name, default=None, required=False):
    value = os.environ.get(name) or file_cfg.get(name) or default
    if required and not value:
        sys.exit(f"Missing required setting: {name} (set it in config.env)")
    return value


def notify_telegram(token, chat_id, text):
    if not (token and chat_id):
        return
    try:
        data = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as exc:  # notification is best-effort, never crash the loop
        log(f"Telegram notify failed: {exc}")


def resolve_image(compute, compartment_id, image_id):
    """Use the configured image, else pick the newest Canonical Ubuntu ARM64 image."""
    if image_id:
        return compute.get_image(image_id).data
    images = compute.list_images(
        compartment_id,
        operating_system="Canonical Ubuntu",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    if not images:
        sys.exit("No Canonical Ubuntu image found for shape " + SHAPE)
    return images[0]


def build_launch_details(cfg, ad_name, image_id, ssh_key_text):
    return oci.core.models.LaunchInstanceDetails(
        display_name=cfg["display_name"],
        compartment_id=cfg["compartment_id"],
        availability_domain=ad_name,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=cfg["ocpus"],
            memory_in_gbs=cfg["memory_gb"],
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=cfg["subnet_id"],
            assign_public_ip=True,
            assign_ipv6_ip=False,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=cfg["boot_volume_gb"],
        ),
        metadata={"ssh_authorized_keys": ssh_key_text},
    )


def wait_for_public_ip(compute, network, compartment_id, instance_id, timeout=600):
    """Poll until the instance is RUNNING, then return its public IP."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        instance = compute.get_instance(instance_id).data
        if instance.lifecycle_state == "RUNNING":
            attachments = compute.list_vnic_attachments(
                compartment_id, instance_id=instance_id
            ).data
            if attachments:
                vnic = network.get_vnic(attachments[0].vnic_id).data
                return vnic.public_ip
        if instance.lifecycle_state in ("TERMINATING", "TERMINATED"):
            return None
        time.sleep(15)
    return None


def precheck(cfg):
    """Fail early with actionable messages on Windows before touching OCI."""
    problems = []
    if "REPLACE_ME" in cfg["subnet_id"]:
        problems.append("SUBNET_ID 仍是 REPLACE_ME 占位符，请填公有子网 OCID")
    if not os.path.exists(cfg["oci_config_file"]):
        problems.append(
            f"找不到 OCI 凭据文件: {cfg['oci_config_file']}\n"
            "        请在 Oracle 控制台生成 API Key，保存到 %USERPROFILE%\\.oci\\config"
        )
    if not cfg["_ssh_key_exists"]:
        problems.append(f"找不到 SSH 公钥: {cfg['_ssh_key_path']}\n"
                        "        请用 ssh-keygen -t ed25519 生成")
    return problems


def main():
    parser = argparse.ArgumentParser(description="Snatch a free OCI A1.Flex instance")
    parser.add_argument("--config", default=os.environ.get(
        "ORACLE_SNATCH_CONFIG", os.path.join(HERE, "config.env")))
    parser.add_argument("--dry-run", action="store_true",
                        help="validate config and print what would be launched, then exit")
    parser.add_argument("--check", action="store_true",
                        help="only check local config/credentials, no OCI calls")
    parser.add_argument("--once", action="store_true",
                        help="run a single round across all ADs then exit")
    args = parser.parse_args()

    file_cfg = load_env_file(args.config)
    if not file_cfg:
        sys.exit(f"Config file not found or empty: {args.config}\n"
                 "Copy config.example.env to config.env and fill it in.")

    key_path = os.path.expanduser(get_setting(
        file_cfg, "SSH_PUBLIC_KEY_FILE", "~/.ssh/id_ed25519.pub"))
    cfg = {
        "subnet_id": get_setting(file_cfg, "SUBNET_ID", required=True),
        "compartment_id": get_setting(file_cfg, "COMPARTMENT_ID"),  # default: tenancy
        "image_id": get_setting(file_cfg, "IMAGE_ID"),
        "display_name": get_setting(file_cfg, "DISPLAY_NAME", "snatch-arm"),
        "ocpus": float(get_setting(file_cfg, "OCPUS", "4")),
        "memory_gb": float(get_setting(file_cfg, "MEMORY_GB", "24")),
        "boot_volume_gb": int(get_setting(file_cfg, "BOOT_VOLUME_GB", "50")),
        "interval": int(get_setting(file_cfg, "INTERVAL_SECONDS", "120")),
        "rate_limit_sleep": int(get_setting(file_cfg, "RATE_LIMIT_SLEEP_SECONDS", "300")),
        "oci_config_file": os.path.expanduser(
            get_setting(file_cfg, "OCI_CONFIG_FILE", "~/.oci/config")),
        "oci_profile": get_setting(file_cfg, "OCI_PROFILE", "DEFAULT"),
        "tg_token": get_setting(file_cfg, "TG_BOT_TOKEN"),
        "tg_chat_id": get_setting(file_cfg, "TG_CHAT_ID"),
        "_ssh_key_path": key_path,
        "_ssh_key_exists": os.path.exists(key_path),
    }

    problems = precheck(cfg)
    if problems:
        print("配置检查未通过：", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 2
    if args.check:
        print("本地配置检查通过（未调用 OCI）。")
        print(f"  OCI config : {cfg['oci_config_file']}")
        print(f"  SSH pub key: {cfg['_ssh_key_path']}")
        print(f"  Subnet     : {cfg['subnet_id']}")
        print(f"  Shape      : {SHAPE} {cfg['ocpus']:g} OCPU / {cfg['memory_gb']:g} GB")
        return 0

    ssh_key_text = open(key_path, encoding="utf-8").read().strip()

    try:
        oci_cfg = oci.config.from_file(cfg["oci_config_file"], cfg["oci_profile"])
        oci.config.validate_config(oci_cfg)
    except ConfigFileNotFound as exc:
        log(f"OCI 凭据文件错误: {exc}")
        return 2
    except Exception as exc:
        log(f"OCI 配置校验失败: {exc}")
        return 2
    if not cfg["compartment_id"]:
        cfg["compartment_id"] = oci_cfg["tenancy"]

    identity = IdentityClient(oci_cfg)
    compute = ComputeClient(oci_cfg)
    network = oci.core.VirtualNetworkClient(oci_cfg)

    try:
        ads = [ad.name for ad in identity.list_availability_domains(
            cfg["compartment_id"]).data]
        image = resolve_image(compute, cfg["compartment_id"], cfg["image_id"])
    except ServiceError as exc:
        log(f"OCI 调用失败 ({exc.status} {exc.code}): {exc.message}")
        log("检查 ~/.oci/config 的 user/tenancy/fingerprint/key_file/region。")
        return 2

    log(f"Region ADs: {', '.join(ads)}")
    log(f"Image: {image.display_name} ({image.id})")
    log(f"Target: {SHAPE} {cfg['ocpus']:g} OCPU / {cfg['memory_gb']:g} GB, "
        f"boot {cfg['boot_volume_gb']} GB, name '{cfg['display_name']}'")

    if args.dry_run:
        log("Dry run OK. Nothing was launched.")
        return 0

    attempt = 0
    while True:
        for ad in ads:
            attempt += 1
            details = build_launch_details(cfg, ad, image.id, ssh_key_text)
            try:
                resp = compute.launch_instance(details)
                instance = resp.data
                log(f"SUCCESS in {ad}: instance {instance.id} "
                    f"({instance.lifecycle_state})")
                ip = wait_for_public_ip(
                    compute, network, cfg["compartment_id"], instance.id)
                result = {
                    "instance_id": instance.id,
                    "display_name": instance.display_name,
                    "availability_domain": ad,
                    "shape": SHAPE,
                    "ocpus": cfg["ocpus"],
                    "memory_gb": cfg["memory_gb"],
                    "public_ip": ip,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                }
                with open(RESULT_FILE, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2)
                log(f"Public IP: {ip}. Details saved to {RESULT_FILE}")
                notify_telegram(cfg["tg_token"], cfg["tg_chat_id"],
                                f"oracle-snatch: got one in {ad}, IP {ip}")
                return 0
            except ServiceError as exc:
                message = (exc.message or "")
                if exc.status == 429:
                    log(f"[{ad}] rate limited (429), sleeping "
                        f"{cfg['rate_limit_sleep']}s")
                    time.sleep(cfg["rate_limit_sleep"])
                elif "capacity" in message.lower():
                    log(f"[{ad}] out of capacity (attempt {attempt}), moving on")
                elif exc.code in ("LimitExceeded", "QuotaExceeded") or \
                        "limit" in message.lower() and "exceeded" in message.lower():
                    log(f"[{ad}] free-tier limit reached: {message}")
                    log("Your Always Free A1 allowance is probably already in "
                        "use. Stopping.")
                    notify_telegram(cfg["tg_token"], cfg["tg_chat_id"],
                                    "oracle-snatch: stopped, free-tier limit reached")
                    return 1
                elif exc.status in (401, 403, 404):
                    log(f"[{ad}] config problem ({exc.status} {exc.code}): {message}")
                    log("Check SUBNET_ID / COMPARTMENT_ID / IMAGE_ID and "
                        "~/.oci/config. Stopping.")
                    return 1
                else:
                    log(f"[{ad}] {exc.status} {exc.code}: {message} "
                        f"(attempt {attempt}), moving on")
            time.sleep(5)
        if args.once:
            log("Single round done (--once). Exiting.")
            return 0
        log(f"Full round done, no capacity anywhere. Sleeping {cfg['interval']}s")
        time.sleep(cfg["interval"])


if __name__ == "__main__":
    sys.exit(main())
