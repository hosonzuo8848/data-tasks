# -*- coding: utf-8 -*-
"""
云端调度核心执行脚本 - dodo.py
职责：
1. 接收 dispatch 传入的环境变量 (分片、Worker 域名、Receiver URL 等)。
2. 先校验接收端 (Receiver) 的 /health 接口是否可通。
3. 对清单中的书籍发起云端拉取 -> 拼 PDF -> 优先 POST 本地 Receiver 直落 -> 可选转码 WebP 并上传至 R2 + D1 注册。
4. 严格输出标准日志阶段标识 [stage] / [fail]。
"""
import os
import sys
import io
import json
import math
import time
import hmac
import hashlib
import random
import csv
import urllib.parse
import urllib.request
from PIL import Image
import fitz
import requests
import boto3

# 禁用全局 SSL 校验，保证 https 通信稳定
import ssl
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

# 载入环境变量与密钥
try:
    SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "20"))
    SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
    MAX_BOOKS = int(os.environ.get("MAX_BOOKS", "5"))
    PAGE_WORKERS = int(os.environ.get("PAGE_WORKERS", "2"))
    R2_WEBP_ENABLED = int(os.environ.get("R2_WEBP", "1")) == 1
    
    # 接收端地址 (由 gap_loop 传入或通过 SB 解析)
    RECEIVER_URL = os.environ.get("RECEIVER_URL", "").strip()
    
    # 解析 Secrets
    HMAC_KEY = os.environ["S1"].encode("utf-8")
    R2_ENDPOINT = os.environ["S2"]
    R2_KEY = os.environ["S3"]
    R2_SECRET = os.environ["S4"]
    R2_BUCKET = os.environ.get("S5", "b1")
    CF_ACCOUNT = os.environ["S6"]
    CF_D1_TOKEN = os.environ["S7"]
    CF_D1_DB = os.environ["S8"]
    REFERER_HOST = os.environ["S9"]
except Exception as e:
    print(f"[fail] env_check. Missing critical env or secret: {e}")
    sys.exit(1)

# 解析 Worker 节点，支持轮询
worker_urls_raw = os.environ.get("WORKER_URLS", "https://2.1fz.dpdns.org,https://2.bnp.indevs.in,https://2.freezz.dpdns.org")
WORKER_URLS = [x.strip() for x in worker_urls_raw.split(",") if x.strip()]

# 默认参数
CL = 3000
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NaikakuCloud/3.0"
LIST_CSV = "data/list.csv"

# 初始化 S3 客户端 (用于 WebP 的 R2 存储)
s3_client = None
if R2_WEBP_ENABLED:
    try:
        s3_client = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT,
            aws_access_key_id=R2_KEY,
            aws_secret_access_key=R2_SECRET,
            region_name="auto"
        )
    except Exception as e:
        print(f"[fail] R2 client initialization failed: {e}")
        R2_WEBP_ENABLED = False

def log_stage(stage):
    print(f"[stage] {stage}", flush=True)

def log_fail(fail_type, msg=""):
    print(f"[fail] {fail_type} | {msg}", flush=True)

# 轮换拉取指定页面的高清分块大图，若某个 Worker 挂了自动换下一个重试
def fetch_ndl_page_tile(tile_path):
    random.shuffle(WORKER_URLS)  # 随机打乱以负载均衡
    for attempt in range(len(WORKER_URLS) * 2):
        worker = WORKER_URLS[attempt % len(WORKER_URLS)]
        target_url = f"{worker}/page?url={urllib.parse.quote(tile_path, safe='')}"
        try:
            req = urllib.request.Request(target_url, headers={
                "User-Agent": UA,
                "Referer": f"https://{REFERER_HOST}/"
            })
            with urllib.request.urlopen(req, timeout=45, context=_SSL) as r:
                data = r.read()
                if data and len(data) > 300:
                    return data
        except Exception:
            time.sleep(1)
    return None

# 云端大图拼接逻辑
def build_canvas_image(service_url, width, height):
    cols = math.ceil(width / CL)
    rows = math.ceil(height / CL)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    success = True
    
    for r in range(rows):
        for c in range(cols):
            x, y = c * CL, r * CL
            w, h = min(CL, width - x), min(CL, height - y)
            tile_path = f"{service_url}/{x},{y},{w},{h}/full/0/default.jpg"
            data = fetch_ndl_page_tile(tile_path)
            if not data:
                success = False
                continue
            try:
                tile_img = Image.open(io.BytesIO(data)).convert("RGB")
                canvas.paste(tile_img, (x, y))
            except Exception:
                success = False
    return canvas, success

# 写入 D1 数据库记录
def register_to_d1(book_id, title, page_count, part, req_no, category_code):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/d1/database/{CF_D1_DB}/query"
    sql = """
    INSERT OR REPLACE INTO books_assets_v2
    (book_id, book_title, webp_prefix, page_count, source_root, source_relative_path, upload_status, webp_status, ocr_status, sueai_status, evidence_status, entity_status, graph_status, rights_status, frontend_visible, collection, req_no, category_code, part, created_at, updated_at)
    VALUES (?, ?, ?, ?, 'x', '', 'done', 'done', 'pending', 'pending', 'pending', 'pending', 'pending', 'public_domain', 1, 'overseas', ?, ?, ?, strftime('%s','now'), strftime('%s','now'))
    """
    params = [book_id, title, f"book/{book_id}/", page_count, req_no, category_code, part]
    
    headers = {
        "Authorization": f"Bearer {CF_D1_TOKEN}",
        "Content-Type": "application/json"
    }
    
    for attempt in range(5):
        try:
            r = requests.post(url, headers=headers, json={"sql": sql, "params": params}, timeout=30)
            if r.status_code == 200 and r.json().get("success"):
                return True
        except Exception as e:
            print(f"[WARN] D1 registration attempt {attempt} failed: {e}")
        time.sleep(2)
    return False

# POST PDF 成品回传本地 (带有 HMAC 签名，支持 3 次重试退避)
def post_pdf_to_receiver(pdf_bytes, part, cat, book, title, vol, sub, pages):
    ts = str(int(time.time()))
    body_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    
    # 构造 HMAC 签名
    msg = f"{ts}\n/upload\n{body_sha256}".encode("utf-8")
    sig = hmac.new(HMAC_KEY, msg, hashlib.sha256).hexdigest()
    
    params = {
        "part": part,
        "cat": cat,
        "book": book,
        "title": title,
        "vol": f"{vol:02d}",
        "sub": sub or "",
        "pages": str(pages),
        "ts": ts,
        "sig": sig
    }
    
    # 3次重试间隔: 10s / 30s / 60s
    retry_intervals = [10, 30, 60]
    for attempt in range(3):
        try:
            r = requests.post(
                f"{RECEIVER_URL}/upload",
                params=params,
                data=pdf_bytes,
                headers={"Content-Type": "application/pdf"},
                timeout=600
            )
            if r.status_code == 200 and r.json().get("ok"):
                return True
            print(f"[WARN] Receiver POST response: {r.status_code} {r.text}")
        except Exception as e:
            print(f"[WARN] Receiver POST attempt {attempt} failed: {e}")
        time.sleep(retry_intervals[attempt])
    return False

# 处理单本书籍的抓取、合并与上传
def process_book(item):
    book_id = item["book_id"]
    vol = int(item["vol"])
    title = item["title"]
    part = item["part"]
    cat = item.get("cat", "医书")
    req_no = item["req_no"]
    category_code = item.get("category_code", "")
    manifest_url = item["manifest"]
    sub_title = ""

    print(f"\n--- 开始处理: {book_id} | {req_no} {title} 册{vol} ---", flush=True)
    log_stage("manifest_load_start")

    # 1. 加载 Manifest
    manifest_data = None
    for attempt in range(4):
        try:
            random.shuffle(WORKER_URLS)
            worker = WORKER_URLS[0]
            target = f"{worker}/fetch?url={urllib.parse.quote(manifest_url, safe='')}"
            req = urllib.request.Request(target, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
                manifest_data = json.loads(r.read().decode("utf-8"))
                break
        except Exception as e:
            print(f"[WARN] Manifest load attempt {attempt} failed: {e}")
            time.sleep(2)

    if not manifest_data:
        log_fail("manifest_load", f"Failed to load manifest: {manifest_url}")
        return False

    log_stage("manifest_load_done")

    # 获取子书名
    lb = manifest_data.get("label", "")
    if isinstance(lb, str):
        sub_title = lb
    elif isinstance(lb, dict):
        for val in lb.values():
            if isinstance(val, list) and val:
                sub_title = str(val[0])
                break

    canvases = manifest_data["sequences"][0]["canvases"]
    total_pages = len(canvases)
    if total_pages == 0:
        log_fail("manifest_load", "Manifest sequences are empty")
        return False

    # 2. 拉取大图并合并大图 PDF
    log_stage("page_fetch_start")
    
    doc = fitz.open()
    fetched_count = 0
    failed_count = 0
    
    # 逐页拉取图片并转为本地大图
    webp_pages_data = [] # 缓存 webp 数据供后续上传
    
    for idx, canvas in enumerate(canvases, 1):
        w = int(canvas.get("width", 0))
        h = int(canvas.get("height", 0))
        service_id = canvas["images"][0]["resource"].get("service", {}).get("@id") or canvas["images"][0]["resource"].get("service", {}).get("id")
        service_url = service_id.rstrip("/") if service_id else ""
        
        if not (service_url and w and h):
            failed_count += 1
            continue
            
        # 拼接图片
        img, ok = build_canvas_image(service_url, w, h)
        if not ok:
            failed_count += 1
            print(f"  [ERROR] Page {idx} failed to build tiles")
            continue
            
        fetched_count += 1
        
        # A. 内存中将大图存入 JPEG 并转换为 PDF 字节流，添加至 fitz 文档
        pdf_temp_io = io.BytesIO()
        img.save(pdf_temp_io, "JPEG", quality=95)
        try:
            temp_pdf_doc = fitz.open(stream=pdf_temp_io.getvalue(), filetype="jpg")
            pdf_block = temp_pdf_doc.convert_to_pdf()
            temp_pdf_doc.close()
            doc.insert_pdf(fitz.open("pdf", pdf_block))
        except Exception as e:
            print(f"  [ERROR] FitZ failed on Page {idx}: {e}")
            failed_count += 1
            
        # B. 存入 WebP 字节流
        if R2_WEBP_ENABLED:
            webp_temp_io = io.BytesIO()
            img.save(webp_temp_io, "WEBP", quality=85, method=4)
            webp_pages_data.append((idx, webp_temp_io.getvalue()))

    if failed_count > 0 or fetched_count == 0:
        log_fail("worker_page_fetch", f"Pages incomplete: Success {fetched_count}, Failed {failed_count}")
        doc.close()
        return False

    log_stage("page_fetch_done")

    # 3. 生成 PDF 字节流
    log_stage("pdf_build_start")
    pdf_bytes = doc.tobytes()
    doc.close()
    log_stage("pdf_build_done")

    # 4. 【最高优先级】回传本地 PDF 接收端
    log_stage("receiver_post_start")
    post_ok = post_pdf_to_receiver(pdf_bytes, part, cat, req_no, title, vol, sub_title, fetched_count)
    if not post_ok:
        log_fail("receiver_post", f"Failed to POST PDF to local receiver: {RECEIVER_URL}")
        return False
    log_stage("receiver_post_done")

    # 5. 可选：WebP 转码并上传 R2 (WebP 报错绝对不阻断流程)
    if R2_WEBP_ENABLED:
        log_stage("webp_convert_start")
        # 已经在拼接时完成转码
        log_stage("webp_convert_done")
        
        log_stage("r2_upload_start")
        r2_failed = False
        for idx, webp_data in webp_pages_data:
            key = f"book/{book_id}-{vol:02d}/page_{idx:04d}.webp"
            # 3次重试
            uploaded = False
            for a in range(3):
                try:
                    s3_client.put_object(Bucket=R2_BUCKET, Key=key, Body=webp_data, ContentType="image/webp")
                    uploaded = True
                    break
                except Exception as e:
                    print(f"  [WARN] R2 upload attempt {a} failed for {key}: {e}")
                    time.sleep(0.5)
            if not uploaded:
                r2_failed = True
                
        if r2_failed:
            print("[WARN] Some WebP files failed to upload to R2, but PDF was successfully delivered.")
        else:
            log_stage("r2_upload_done")
            
        # 6. D1 数据库登记注册
        log_stage("d1_register_start")
        d1_ok = register_to_d1(f"{book_id}-{vol:02d}", title, fetched_count, part, req_no, category_code)
        if not d1_ok:
            print("[WARN] D1 registration failed, but PDF was successfully delivered.")
        else:
            log_stage("d1_register_done")

    return True

# 本地接收端健康探测
def verify_receiver_health():
    log_stage("receiver_healthcheck")
    if not RECEIVER_URL:
        print("[fail] receiver_post | RECEIVER_URL is empty")
        sys.exit(1)
        
    try:
        url = f"{RECEIVER_URL}/health"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15, context=_SSL) as r:
            res = json.loads(r.read().decode("utf-8"))
            if res.get("ok"):
                print("Receiver health check OK")
                return True
    except Exception as e:
        print(f"[fail] receiver_post | Health check failed on {RECEIVER_URL}: {e}")
        sys.exit(1)
    return False

def main():
    log_stage("env_check")
    verify_receiver_health()

    # 读取 CSV 清单
    if not os.path.exists(LIST_CSV):
        print(f"[fail] List CSV not found: {LIST_CSV}")
        sys.exit(1)

    rows = []
    with open(LIST_CSV, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    # 提取过滤出分配给当前 Shard 的任务
    shard_items = [row for idx, row in enumerate(rows) if idx % SHARD_COUNT == SHARD_INDEX]
    print(f"Shard {SHARD_INDEX}/{SHARD_COUNT} has total {len(shard_items)} items")

    # 限额控制
    target_items = shard_items[:MAX_BOOKS]
    print(f"Processing limit: {len(target_items)} items")

    success_count = 0
    results = []
    
    for item in target_items:
        ok = False
        try:
            ok = process_book(item)
        except Exception as e:
            print(f"[ERROR] Fatal exception processing book {item['book_id']}: {e}")
        
        results.append({
            "book_id": item["book_id"],
            "vol": item["vol"],
            "ok": ok
        })
        if ok:
            success_count += 1
            
    # 输出结果报告
    with open("r.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(results, ensure_ascii=False))

    print(f"\nShard Run Finished. Success: {success_count}/{len(target_items)}", flush=True)

if __name__ == "__main__":
    main()
