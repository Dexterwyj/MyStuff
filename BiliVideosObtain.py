import os
import re
import sys
import json
import shutil
import subprocess
import requests
import time
import hashlib
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qs

# ================= 配置区 =================
BVID = "BV1ujL3zKEyd"
PAGE = 1
OUTPUT_DIR = r"D:\video\bilibili"
PREFER_CODECID = 7
# 可选：浏览器 F12 → Application → Cookies 里复制 SESSDATA 的值（保留原始编码，别解码）
# 留空则以游客身份请求，画质最高约 480P；填写后可拿到 1080P
SESSDATA = ""
# ==========================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
}

if SESSDATA:
    HEADERS["Cookie"] = f"SESSDATA={SESSDATA}"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

def get_cid(bvid: str, page: int = 1) -> int:
    """调用view接口按P获取视频的cid"""
    url = "https://api.bilibili.com/x/web-interface/view"
    resp = requests.get(url, params={"bvid": bvid}, headers=HEADERS,timeout=60)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError("view 接口返回的不是合法 JSON") from e
    if payload.get("code") != 0:
        raise RuntimeError(f"获取视频cid失败: {payload.get('msg', '未知错误')}")
    data = payload.get("data") or {}
    pages = data.get("pages") or []
    if not pages:
        raise RuntimeError("view 响应中没有 pages 数据。")
    if page < 1 or page > len(pages):
        raise RuntimeError(f"分P {page} 超出范围（该视频共 {len(pages)} P）。")

    p = pages[page - 1]
    cid = p.get("cid")
    if not isinstance(cid, int):
        raise RuntimeError(f"分P {page} 的 cid 无效: {cid!r}")

    print(f"分P {page}/{len(pages)}: {p.get('part', '(无标题)')}")
    return cid

def _extract_key(url: str, field_name: str) -> str:
    """从完整 URL 中提取去掉扩展名的文件名，作为 WBI key。"""
    path = urlparse(url).path
    filename = os.path.basename(path)
    if not filename:
        raise RuntimeError(f"{field_name} 的 URL 中没有文件名: {url!r}")

    key, sep, ext = filename.rpartition(".")
    if not sep or not key:
        raise RuntimeError(
            f"{field_name} 的文件名格式异常，缺少扩展名或主名: {filename!r}"
        )
    return key

def get_wbi_keys() -> tuple[str, str]:
    """从nav接口获取img_key和sub_key"""
    url = "https://api.bilibili.com/x/web-interface/nav"
    resp = requests.get(url, headers=HEADERS,timeout=60)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError("nav 接口返回的不是合法 JSON") from e

    # 未登录时 nav 返回 -101，但 data.wbi_img 依然是有效的
    if payload.get("code") not in (0, -101):
        raise RuntimeError(
            f"nav 接口返回错误: code={payload.get('code')}, "
            f"message={payload.get('message')}"
        )

    data = payload.get("data") or {}
    wbi_img = data.get("wbi_img")
    if not wbi_img:
        raise RuntimeError("nav 响应中没有 wbi_img 字段")
    
    img_url = wbi_img.get("img_url")
    sub_url = wbi_img.get("sub_url")
    if not img_url or not sub_url:
        raise RuntimeError("nav 响应中没有 img_url 或 sub_url 字段")
    
    img_key = _extract_key(img_url,"img_key")
    sub_key = _extract_key(sub_url,"sub_key")
    return img_key, sub_key
    
def get_mixin_key(img_key:str, sub_key:str) -> str:
    """将img_key和sub_key按混淆表重排，截取前32位得到mixin_key"""
    raw = img_key +sub_key
    if len(raw) < 64:
        raise RuntimeError(
            f"mixin_key原料不足64：img_key={len(img_key)},"
            f"sub_key={len(sub_key)}"
        )
    shuffled = "".join([raw[i] for i in MIXIN_KEY_ENC_TAB])
    return shuffled[:32]

def sign_wbi(params: dict, mixin_key: str) -> dict:
    """
    对请求参数进行 WBI 签名。
    将参数按 key 字母序排序，追加 mixin_key，做 MD5 得到 w_rid，
    同时添加当前时间戳 wts。
    """
    # 官方要求：签名前剔除参数值中的 !'()* 字符
    params = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in params.items()
    }
    params["wts"] = int(time.time())
    sorted_params = dict(sorted(params.items()))
    query = urlencode(sorted_params)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    sorted_params["w_rid"] = w_rid
    return sorted_params

def get_playurl_dash(bvid: str, cid: int) -> dict:
    """调用 playurl API，获取 DASH 数据，带响应结构校验。"""
    img_key, sub_key = get_wbi_keys()
    mixin_key = get_mixin_key(img_key, sub_key)

    params = {
        "bvid": bvid,
        "cid": cid,
        "fnval": 16,      # DASH
        "qn": 80,         # 请求 1080P
        "try_look": 1,    # 游客尝试高画质
    }
    signed = sign_wbi(params, mixin_key)

    url = "https://api.bilibili.com/x/player/wbi/playurl"
    resp = requests.get(url, headers=HEADERS, params=signed, timeout=30)
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as e:
        raise RuntimeError("playurl 接口返回的不是合法 JSON") from e

    if payload.get("code") != 0:
        raise RuntimeError(
            f"playurl API 返回错误: code={payload.get('code')}, "
            f"message={payload.get('message')}"
        )

    data = payload.get("data") or {}
    dash = data.get("dash")
    if not isinstance(dash, dict) or not dash:
        voucher = data.get("v_voucher")
        hint = "（收到 v_voucher 风控挑战，签名可能未被接受）" if voucher else ""
        raise RuntimeError(f"响应中没有有效的 dash 数据{hint}。")

    # 记录服务端实际给的画质，方便排查“为什么只有 360P”
    quality = data.get("quality")
    accept = data.get("accept_quality") or []
    if quality is not None:
        print(f"服务端返回画质 id: {quality}（可选: {accept}）")

    return dash


def pick_stream(dash: dict, stream_type: str,
                prefer_codecid: Optional[int] = None) -> dict:
    """
    从 dash 中挑选一条流。
    - 若指定 prefer_codecid，优先选该编码；找不到再回退。
    - 同一编码内按 bandwidth 取最大。
    """
    streams = dash.get(stream_type)
    if not isinstance(streams, list) or not streams:
        raise RuntimeError(f"dash 中没有有效的 {stream_type} 流。")

    candidates = streams
    if prefer_codecid is not None:
        matched = [s for s in streams if s.get("codecid") == prefer_codecid]
        if matched:
            candidates = matched

    return max(candidates, key=lambda s: s.get("bandwidth", 0))


def get_stream_url(stream_info: dict) -> str:
    """取 baseUrl，回退到 backupUrl，并校验结果是 http(s) URL。"""
    url = stream_info.get("baseUrl")
    if not url:
        backup = stream_info.get("backupUrl")
        if isinstance(backup, list) and backup:
            url = backup[0]

    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RuntimeError(f"该流没有可用的 http(s) URL: {url!r}")
    return url


def download_file(url: str, filepath: str, desc: str = ""):
    """流式下载，并显示简单进度。"""
    print(f"开始下载 {desc} ...")
    with requests.get(url, headers=HEADERS, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r  {desc}: {pct:5.1f}%", end="", flush=True)
    print(f"\n{desc} 完成: {filepath}")


def get_ffmpeg_exe() -> str:
    """优先使用打包进 exe 的 ffmpeg，其次用系统 PATH 中的。"""
    if getattr(sys, "frozen", False):
        bundled = os.path.join(sys._MEIPASS, "ffmpeg.exe")
        if os.path.isfile(bundled):
            return bundled
    return shutil.which("ffmpeg") or "ffmpeg"


def parse_video_input(raw: str) -> tuple[str, Optional[int]]:
    """
    支持 BV 号、视频链接、b23.tv 短链。
    返回 (bvid, 链接中的分P号)，分P不存在时为 None。
    """
    text = raw.strip().strip('"').strip("'")
    match = re.search(r"(?i:BV)[0-9A-Za-z]{10}", text)

    # 短链等没有 BV 号的链接，跟随跳转后再解析
    if not match and text.lower().startswith(("http://", "https://")):
        resp = requests.get(text, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        text = resp.url
        match = re.search(r"(?i:BV)[0-9A-Za-z]{10}", text)

    if not match:
        raise RuntimeError(f"无法从输入中识别 BV 号: {raw!r}")

    bvid = "BV" + match.group(0)[2:]

    page = None
    if text.lower().startswith(("http://", "https://")):
        p_values = parse_qs(urlparse(text).query).get("p")
        if p_values and p_values[0].isdigit():
            page = int(p_values[0])

    return bvid, page


def merge_with_ffmpeg(video_path: str, audio_path: str, output_path: str):
    """调用 FFmpeg 无损合并音视频。"""
    cmd = [
        get_ffmpeg_exe(), "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "copy",
        "-map", "0:v:0",
        "-map", "1:a:0",
        output_path,
    ]
    print("正在调用 FFmpeg 合并 ...")
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError:
        raise RuntimeError("找不到 ffmpeg 命令，请确认已安装并在 PATH 中。")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"FFmpeg 合并失败:\n{e.stderr}") from e
    print(f"合并完成: {output_path}")


def main():
    raw = input("请输入视频 BV 号或视频链接: ")
    BVID, url_page = parse_video_input(raw)
    _page = input(f"请输入分P号（默认 {url_page or 1}）: ").strip()
    PAGE = int(_page) if _page else (url_page or 1)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print(f"BV 号: {BVID}")
    cid = get_cid(BVID, PAGE)
    print(f"cid: {cid}")

    dash = get_playurl_dash(BVID, cid)
    video_stream = pick_stream(dash, "video", prefer_codecid=PREFER_CODECID)
    audio_stream = pick_stream(dash, "audio")

    print(
        f"视频流: {video_stream.get('width')}x{video_stream.get('height')}, "
        f"codecid={video_stream.get('codecid')}, "
        f"codecs={video_stream.get('codecs')}, "
        f"bandwidth={video_stream.get('bandwidth')}"
    )
    print(
        f"音频流: id={audio_stream.get('id')}, "
        f"codecs={audio_stream.get('codecs')}, "
        f"bandwidth={audio_stream.get('bandwidth')}"
    )

    video_url = get_stream_url(video_stream)
    audio_url = get_stream_url(audio_stream)

    prefix = f"{BVID}_P{PAGE}"
    video_path = os.path.join(OUTPUT_DIR, f"{prefix}_video.m4s")
    audio_path = os.path.join(OUTPUT_DIR, f"{prefix}_audio.m4s")
    final_path = os.path.join(OUTPUT_DIR, f"{prefix}.mp4")

    download_file(video_url, video_path, desc="视频流")
    download_file(audio_url, audio_path, desc="音频流")
    merge_with_ffmpeg(video_path, audio_path, final_path)

    os.remove(video_path)
    os.remove(audio_path)

    print(f"\n全部完成: {os.path.abspath(final_path)}")


if __name__ == "__main__":
    main()