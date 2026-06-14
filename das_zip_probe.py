# 【CC下载线·内閣BID攻关·云端实测脚本】
# 目的:在海外 IP(GitHub Actions runner)上验证内閣官方打包下载链路:
#   IIIF id --(本地/CDN可达)--> manifest --> DAS BID(F1...)
#   --(海外IP)--> listPhoto(BID) --> 各冊 M-id --> auto_conversion/download --> 整冊 ZIP
# 复刻 bookget app/nationaljp.go 的协议(cookiejar 维持会话)。只验证 1 本,不量产。
# 用法: python das_zip_probe.py <IIIF_ID>            (默认 1079184 飲膳正要 155页单件名)
import sys, re, os, time, json, urllib.parse, requests

IIIF_ID = sys.argv[1] if len(sys.argv) > 1 else "1079184"
HOST = "www.digital.archives.go.jp"
BASE = f"https://{HOST}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
OUT = os.environ.get("OUT_DIR", ".")
os.makedirs(OUT, exist_ok=True)

S = requests.Session()                 # 单 Session = 复刻 bookget cookiejar(listPhoto 种 cookie,download 带上)
S.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})

def log(*a): print(*a, flush=True)

# ---- 1) IIIF manifest -> DAS BID (这一步海外/国内都可,CDN 可达) ----
murl = f"{BASE}/api/iiif/{IIIF_ID}/manifest.json"
log(f"[1] manifest: {murl}")
m = S.get(murl, timeout=60); m.raise_for_status()
mt = m.text
bids = sorted(set(re.findall(r"(F[01]\d{18})", mt)))
ncanvas = len(json.loads(mt).get("sequences", [{}])[0].get("canvases", []))
log(f"    pages(canvas)={ncanvas}  BID candidates={bids}")
if not bids:
    log("FATAL: no BID in manifest"); sys.exit(2)
BID = bids[0]
log(f"    => DAS BID = {BID}")

# 先访问 viewer/file 页种 cookie(有些 DAS 后端要求先有会话)
for warm in (f"{BASE}/file/{IIIF_ID}", f"{BASE}/img/{IIIF_ID}"):
    try:
        r = S.get(warm, timeout=45)
        log(f"[warm] {warm} -> {r.status_code} (cookies now: {list(S.cookies.keys())})")
    except Exception as e:
        log(f"[warm] {warm} ERR {e}")

# ---- 2) listPhoto(BID) -> 各冊 M-id ----
# bookget: GET /DAS/meta/listPhoto?LANG=default&BID=..&ID=&NO=&TYPE=dljpeg&DL_TYPE=jpeg
lp = f"{BASE}/DAS/meta/listPhoto?LANG=default&BID={BID}&ID=&NO=&TYPE=dljpeg&DL_TYPE=jpeg"
log(f"[2] listPhoto: {lp}")
try:
    r = S.get(lp, headers={"Referer": f"{BASE}/file/{IIIF_ID}"}, timeout=90)
    log(f"    HTTP {r.status_code}  len={len(r.text)}")
    open(os.path.join(OUT, f"listPhoto_{BID}.html"), "w", encoding="utf-8").write(r.text)
    html = r.text
except Exception as e:
    log(f"    listPhoto ERR {e}"); sys.exit(3)

if "HTTP Status 404" in html or r.status_code == 404:
    log("    !! 404 — BID 不被 DAS 接受 或 该路径地理封锁。见报告诊断。")
# bookget 正则:<input ... posi="N" ... value="M....">
matches = re.findall(r'<input[^>]+posi=["\']([0-9]+)["\'][^>]+value=["\']([A-Za-z0-9]+)["\']', html)
log(f"    posi/value(冊) matches = {len(matches)}: {matches[:10]}")
# 兜底:任意 M-id
mids_any = sorted(set(re.findall(r'(M[0-9A-Za-z]{15,})', html)))
log(f"    any M-ids in page = {mids_any[:10]}")

vols = []
for posi, val in matches:
    if len(matches) > 1 and (posi == "0" or val == ""):  # 跳过全选框
        continue
    vols.append(val)
if not vols:
    vols = mids_any   # 兜底
log(f"    => volume(冊) M-ids = {vols[:10]} (total {len(vols)})")
if not vols:
    log("FATAL: listPhoto 没拿到任何冊 M-id。"); sys.exit(4)

# ---- 3) auto_conversion/download(M-id) -> 整冊 ZIP ----
# bookget: POST /acv/auto_conversion/download  body: DL_TYPE=jp2&id_{index}={M-id}
dl = f"{BASE}/acv/auto_conversion/download"
for i, vid in enumerate(vols[:1], 1):     # 只验第 1 冊
    body = f"DL_TYPE=jp2&id_{i}={vid}"
    log(f"[3] download POST {dl}  body={body}")
    try:
        r = S.post(dl, data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded",
                            "Referer": lp},
                   timeout=600, stream=True)
        ct = r.headers.get("Content-Type", "")
        cl = r.headers.get("Content-Length", "?")
        log(f"    HTTP {r.status_code}  Content-Type={ct}  Content-Length={cl}")
        dest = os.path.join(OUT, f"{BID}_vol{i:04d}.zip")
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                if chunk:
                    f.write(chunk); total += len(chunk)
        log(f"    wrote {dest}  bytes={total}")
        head = open(dest, "rb").read(4)
        log(f"    magic={head!r}  is_zip={head[:2]==b'PK'}")
        if head[:2] != b"PK":
            log("    !! 不是 ZIP(可能是 HTML 错误页/重定向)。前 300 字节:")
            log(open(dest, "rb").read(300))
    except Exception as e:
        log(f"    download ERR {e}")

log("DONE")
