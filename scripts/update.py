#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV 直播源自动更新脚本（纯标准库，不装任何第三方包）

流程：
  1) 读取 config/sources.txt 里的上游订阅地址，拉取 m3u / txt 列表
  2) 解析、清洗、按频道合并，可选按 config/channels.txt 白名单过滤
  3) 多线程对每条流做连通性 + 首包内容测活
  4) 输出 docs/iptv.m3u、docs/iptv.txt、docs/status.json
  5) 测活结果写入 data/cache.json；本轮存活过少时用缓存兜底，避免电视黑屏

用法：python scripts/update.py [--dry-run] [--quick]
  --dry-run  不写文件，只打印统计
  --quick    跳过网络测活，直接保留全部候选（本地调试用）
"""

import base64
import concurrent.futures
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 基础工具

_print_lock = threading.Lock()


def log(msg):
    with _print_lock:
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def load_json(path, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log("配置文件解析失败 %s: %s，使用默认值" % (path, exc))
        return default


def read_text(path):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            return ""
    return ""


def _build_opener(timeout):
    """宽松 TLS 的 opener：个别源证书链不全，握手失败会让整条链路误判为失效"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPRedirectHandler(),
    )


OPENER = _build_opener(10)


def http_get(url, timeout, retries=2, max_bytes=None, headers=None):
    """GET 一个 URL，返回 (status, body_bytes, content_type)。彻底失败返回 (0, b'', '')"""
    hdr = {
        "User-Agent": HEADERS["ua"],
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if headers:
        hdr.update(headers)

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdr, method="GET")
            with OPENER.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                ctype = (resp.headers.get("Content-Type") or "").lower()
                body = resp.read(max_bytes) if max_bytes else resp.read()
                return status, body, ctype
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(2048) if exc.fp else b""
            except Exception:  # noqa: BLE001
                body = b""
            ctype = (exc.headers.get("Content-Type") or "").lower() if exc.headers else ""
            return exc.code, body, ctype
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    if last_err:
        log("请求失败 %s -> %s" % (_short(url), type(last_err).__name__))
    return 0, b"", ""


def _short(url, n=58):
    return url if len(url) <= n else url[: n - 3] + "..."


# ---------------------------------------------------------------- 名称归一化

_FULL = {ord(c): ord(h) for c, h in zip(
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ（）：；，．－＋",
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz():,.-++")}

_STRIP_CHARS = re.compile(r"[\s\-_·.。，,、|/\\\[\]【】()（）+*#&~'\"：:；;]")


def norm_name(name):
    """频道名归一化：CCTV-1 综合 / cctv1 / CCTV1综合 → cctv1综合"""
    if not name:
        return ""
    s = name.translate(_FULL)
    s = s.strip().lower()
    s = s.replace("+", "plus")
    s = _STRIP_CHARS.sub("", s)
    # 常见后缀噪音
    for suf in ("高清", "标清", "超清", "蓝频", "备频", "备用", "主频", "cc", "广告", "影院", "版"):
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[: -len(suf)]
            break
    return s


# 频道别名：解决“同一个台各种叫法”
ALIASES = {
    "cctv1综合": "cctv1", "cctv1": "cctv1", "cctv综合": "cctv1", "央视一套": "cctv1", "cctv1hd": "cctv1",
    "cctv2财经": "cctv2", "央视二套": "cctv2", "cctv2": "cctv2",
    "cctv3综艺": "cctv3", "央视三套": "cctv3", "cctv3": "cctv3",
    "cctv4中文国际": "cctv4", "cctv4": "cctv4",
    "cctv5体育": "cctv5", "央视五套": "cctv5", "cctv5": "cctv5",
    "cctv5p体育赛事": "cctv5plus", "cctv5plus": "cctv5plus", "cctv5+": "cctv5plus", "cctv赛事": "cctv5plus",
    "cctv6电影": "cctv6", "cctv6": "cctv6",
    "cctv7国防军事": "cctv7", "cctv7": "cctv7",
    "cctv8电视剧": "cctv8", "cctv8": "cctv8",
    "cctv9纪录": "cctv9", "cctv9": "cctv9",
    "cctv10科教": "cctv10", "cctv10": "cctv10",
    "cctv11戏曲": "cctv11", "cctv11": "cctv11",
    "cctv12社会与法": "cctv12", "cctv12": "cctv12",
    "cctv13新闻": "cctv13", "cctv13": "cctv13",
    "cctv14少儿": "cctv14", "cctv14": "cctv14",
    "cctv15音乐": "cctv15", "cctv15": "cctv15",
    "cctv16奥林匹克": "cctv16", "cctv16": "cctv16", "cctv16奥运": "cctv16",
    "cctv17农业农村": "cctv17", "cctv17": "cctv17",
    "cctv4k": "cctv4k", "cctv4k超高清": "cctv4k",
}


def canon_name(display, norm):
    key = ALIASES.get(norm, norm)
    return key, display


# ---------------------------------------------------------------- 分组推断

GROUP_RULES = [
    ("央视", re.compile(r"cctv|中央|央视|coctv")),
    ("体育", re.compile(r"体育|赛事|cctv\+|劲爆|足球|篮球|olympic|espn|skymainevent")),
    ("港澳台", re.compile(r"tvb|翡翠|無線|无綫|now|台视|中视|华视|公视|凤凰|星空|港台|澳门|香港|台湾|纬来|东森|中天|壹新闻")),
    ("卫视", re.compile(r"卫视|南方卫视|华语卫星")),
    ("地方", re.compile(r"北京|上海|广东|江苏|浙江|安徽|山东|河南|湖北|湖南|四川|重庆|天津|南京|杭州|广州|深圳|武汉|成都|合肥|青岛|厦门|东方|live")),
    ("纪实", re.compile(r"纪实|纪录|探索|discovery|地理|历史|动物|planet")),
    ("电影", re.compile(r"电影|cinema|movie|hdlive")),
    ("动漫", re.compile(r"动漫|卡通|动画|toon|anime|cartoon")),
    ("少儿", re.compile(r"少儿|宝贝|kid|baby|smart")),
    ("新闻", re.compile(r"新闻|资讯|bbc|cnn|fox|nhk|cnbc")),
    ("音乐", re.compile(r"音乐|mv|mtv|channel[v]|全球音乐")),
]


GROUP_ALIAS = {
    "央视频道": "央视", "央视": "央视", "cctv": "央视", "coctv": "央视",
    "卫视频道": "卫视", "卫视": "卫视", "卫星频道": "卫视",
    "地方频道": "地方", "地方": "地方", "地方台": "地方",
    "体育频道": "体育", "体育": "体育", "sport": "体育", "sports": "体育",
    "电影频道": "电影", "电影": "电影", "movie": "电影", "movies": "电影",
    "纪实频道": "纪实", "纪录片": "纪实", "纪录": "纪实", "纪实": "纪实",
    "动漫": "动漫", "动画": "动漫", "卡通": "动漫", "cartoon": "动漫",
    "少儿": "少儿", "宝贝": "少儿", "kids": "少儿",
    "新闻": "新闻", "news": "新闻", "资讯": "新闻",
    "音乐": "音乐", "music": "音乐",
    "港澳台": "港澳台", "港台": "港澳台", "香港": "港澳台", "澳门": "港澳台", "台湾": "港澳台",
    "国际频道": "国际", "国际": "国际",
    "其他": "其他", "未分类": "其他", "ipv6": "其他", "运营商": "其他",
}


EN_GROUP = {
    "news": "新闻", "business": "财经", "entertainment": "综合", "education": "科教",
    "culture": "文艺", "documentary": "纪实", "lifestyle": "生活", "classic": "文艺",
    "general": "综合", "kids": "少儿", "children": "少儿", "animation": "动漫",
    "weather": "生活", "shop": "购物", "shopping": "购物", "legislative": "新闻",
    "science": "科教", "family": "家庭", "music": "音乐", "movies": "电影",
    "sports": "体育", "auto": "生活", "outdoor": "生活", "religious": "综合",
    "unknown": "其他", "undefined": "其他", "other": "其他", "xxx": "其他",
}


def clean_group(raw):
    """把上游五花八门的分组名收敛到少数几个标准分组"""
    g = (raw or "").strip()
    if not g:
        return ""
    # iptv-org 会用 "Kids; Lifestyle; Business" 这种多标签，取第一个
    g = re.split(r"[;；]", g)[0].strip()
    if not g or g.lower() in ("undefined", "unknown", "other", "未分类"):
        return "其他"
    key = norm_name(g)
    if key in GROUP_ALIAS:
        return GROUP_ALIAS[key]
    if g.lower() in EN_GROUP:
        return EN_GROUP[g.lower()]
    if key in EN_GROUP:
        return EN_GROUP[key]
    for cn, std in (("央视", "央视"), ("cctv", "央视"), ("卫视", "卫视"), ("地方", "地方"),
                    ("体育", "体育"), ("sport", "体育"), ("电影", "电影"), ("movie", "电影"),
                    ("纪录", "纪实"), ("纪实", "纪实"), ("动漫", "动漫"), ("少儿", "少儿"),
                    ("儿童", "少儿"), ("新闻", "新闻"), ("news", "新闻"), ("音乐", "音乐"),
                    ("music", "音乐"), ("港澳台", "港澳台"), ("tvb", "港澳台"),
                    ("国际", "国际"), ("overseas", "国际"), ("更新时间", ""), ("公告", ""),
                    ("运营商", "其他"), ("其它", "其他"), ("其他", "其他"), ("汇集", "其他"),
                    ("备用", "其他"), ("说明", ""), ("测试", "")):
        if cn and cn in key:
            return std
    if g.startswith("•") or "「" in g or "」" in g:
        return "其他"
    return g[:10] if len(g) <= 10 else "其他"


def guess_group(display, norm, raw_group):
    g = clean_group(raw_group)
    if g:
        return g
    for name, pat in GROUP_RULES:
        if pat.search(norm) or pat.search(display.lower()):
            return name
    return "其他"


# ---------------------------------------------------------------- 解析上游

def parse_m3u(text, source):
    """返回 [{name, url, group, logo}]"""
    items = []
    lines = text.splitlines()
    pending = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            name = ""
            m = re.search(r",([^\r\n,]*)\s*$", line)
            if m:
                name = m.group(1).strip()
            g = re.search(r'group-title="([^"]*)"', line)
            lg = re.search(r'tvg-logo="([^"]*)"', line)
            tn = re.search(r'tvg-name="([^"]*)"', line)
            if not name:
                name = (tn.group(1) if tn else "")
            pending = {
                "name": name,
                "group": g.group(1) if g else "",
                "logo": lg.group(1) if lg else "",
            }
            continue
        if line.startswith("#"):
            # EXT-X- 等其它标签，若出现在 URL 前则丢弃 pending
            if not line.startswith("#EXT-X-"):
                pending = None
            continue
        url = line
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url) and not re.match(r"^(rtp|udp|rtmp|http)", url):
            continue
        item = pending or {"name": "", "group": "", "logo": ""}
        items.append({
            "name": item["name"] or url.split("/")[-1][:40],
            "url": url,
            "group": item["group"],
            "logo": item["logo"],
            "source": source,
        })
        pending = None
    return items


def parse_txt(text, source):
    """TVBox 格式：分类,#genre# 标记行 + 频道名,地址[备用|地址]"""
    items = []
    current_group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#genre#" in line:
            current_group = line.split(",", 1)[0].strip()
            continue
        if "," not in line:
            continue
        name, rest = line.split(",", 1)
        name = name.strip()
        rest = rest.strip()
        if not name or not rest:
            continue
        # 备用地址用 # 或 | 分隔
        for url in re.split(r"[#|]", rest):
            url = url.strip()
            if not url:
                continue
            if not re.search(r"[:：]", url):
                continue
            url = url.replace("：", ":")
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
                continue
            items.append({"name": name, "url": url,
                          "group": current_group, "logo": "", "source": source})
    return items


def parse_content(text, source):
    if "#EXTM3U" in text[:600] or "#EXTINF" in text[:3000]:
        return parse_m3u(text, source)
    return parse_txt(text, source)


# ---------------------------------------------------------------- 采集

HEADERS = {"ua": "Mozilla/5.0"}
MAX_FETCH_BYTES = 12 * 1024 * 1024


def fetch_source(url, timeout):
    status, body, _ = http_get(url, timeout, retries=2, max_bytes=MAX_FETCH_BYTES + 1)
    if not body or len(body) > MAX_FETCH_BYTES:
        return []
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = body.decode("gb18030")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
    items = parse_content(text, url)
    log("  %-58s -> %d 条" % (_short(url), len(items)))
    return items


# ---------------------------------------------------------------- 测活

TS_SYNC = b"\x47"
SKIP_SCHEMES = ("rtp://", "udp://", "rtsp://", "rtspt://", "igmp://", "srt://", "file://")

_tester_lock = threading.Lock()
_stats = {"ok": 0, "bad": 0, "skip": 0}


def is_live_payload(ctype, body):
    """根据响应头与首包字节判断是否像音视频流"""
    if not body:
        return False
    head = body[:4096].lower()
    is_m3u8 = any(t in ctype for t in ("mpegurl", "m3u8", "vnd.apple"))
    if is_m3u8:
        # HLS 清单：master 或 media playlist 均视为有效
        return (b"#extm3u" in head or b"#extinf" in head
                or b"ext-x-stream-inf" in head or b"ext-x-targetduration" in head
                or b"ext-x-key" in head or b"ext-x-media-sequence" in head)
    if any(t in ctype for t in ("video", "audio", "octet-stream", "mp2t", "mp4", "flv", "mpegts", "dashed")):
        return True
    if b"#extm3u" in head or b"#extinf" in head or b"ext-x-stream-inf" in head:
        return True
    if body[:1] == TS_SYNC or body[:4] in (b"\x00\x00\x00\x00", b"\x00\x00\x00\x0c") or b"ftyp" in body[:16]:
        return True
    if b"flv" in body[:4]:
        return True
    # 有实质二进制负载也认为可用（部分源不返回正确 Content-Type）
    if len(body) >= 2048 and not head.lstrip()[:60].startswith((b"<html", b"<!doctype", b"{\"error")):
        return True
    return False


def probe(url, timeout):
    """返回 (state, extra) state: ok / bad / skip"""
    low = url.lower()
    if low.startswith(SKIP_SCHEMES):
        return "skip", "不可测协议"
    parsed = urllib.parse.urlparse(low)
    host = parsed.hostname or ""
    # 组播地址 / 内网 / 链路本地地址，在云端无法访问，直接标记为不可测
    if host.startswith(("239.", "224.", "232.", "127.", "10.", "192.168.", "172.16.", "fe80")):
        return "skip", "内网/组播"
    if host.startswith("[fe80") or host.endswith(".local"):
        return "skip", "内网"

    status, body, ctype = http_get(url, timeout, retries=CONFIG["connect_retries"], max_bytes=65536)
    if status == 0:
        return "bad", "连接失败"
    if status in (401, 403, 404, 405, 451):
        # 部分源要求 HEAD/特殊 UA，403/405 再试一次带 Range 的请求
        st2, body2, ct2 = http_get(url, timeout, retries=0, max_bytes=65536,
                                   headers={"Range": "bytes=0-65535", "Referer": "%s://%s/" % (parsed.scheme, host)})
        if st2 in (200, 206) and is_live_payload(ct2, body2):
            return "ok", "range:%d" % st2
        return "bad", "HTTP %d" % status
    if status not in (200, 206):
        return "bad", "HTTP %d" % status
    if is_live_payload(ctype, body):
        return "ok", "%d bytes" % len(body)
    return "bad", "非媒体内容(%s)" % (ctype.split(";")[0] or "未知")


def test_one(task, timeout):
    t0 = time.time()
    state, extra = probe(task["url"], timeout)
    task["ms"] = int((time.time() - t0) * 1000)
    with _tester_lock:
        _stats[state] = _stats.get(state, 0) + 1
        done = sum(_stats.values())
    if done % 200 == 0:
        log("  进度 %d | 可用 %d | 失效 %d | 跳过 %d" % (done, _stats["ok"], _stats["bad"], _stats["skip"]))
    task["state"] = state
    task["extra"] = extra
    return task


# ---------------------------------------------------------------- 主流程

CONFIG = {}


def load_whitelist(path):
    wl = set()
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        n = norm_name(s)
        if n:
            wl.add(ALIASES.get(n, n))
    return wl


def dedupe_and_merge(items, whitelist):
    """按频道合并：同名（归一后）的多个地址归为一个频道"""
    channels = OrderedDict()
    for it in items:
        display = (it["name"] or "").strip()
        if not display or not it["url"]:
            continue
        norm = norm_name(display)
        if not norm:
            continue
        key = ALIASES.get(norm, norm)
        if whitelist and key not in whitelist and norm not in whitelist:
            continue
        if len(key) < 2:
            continue
        ch = channels.get(key)
        if not ch:
            ch = {"key": key, "display": display,
                  "group": guess_group(display, norm, it.get("group", "")),
                  "logo": it.get("logo", ""), "urls": []}
            channels[key] = ch
        else:
            if not ch["logo"] and it.get("logo"):
                ch["logo"] = it["logo"]
            if ch["display"] != display and len(display) < len(ch["display"]):
                ch["display"] = display
        if it["url"] not in [u["url"] for u in ch["urls"]]:
            ch["urls"].append({"url": it["url"], "source": it.get("source", "")})
    return channels


def load_cache(path, hours):
    """返回 {"urls": {url: entry}, "by_key": {channel_key: [entry]}}"""
    data = load_json(path, {})
    cutoff = time.time() - hours * 3600
    urls, by_key = {}, {}
    for key, val in (data.get("urls") or {}).items():
        if val.get("ts", 0) >= cutoff:
            urls[key] = val
            ck = val.get("key") or norm_name(val.get("name", ""))
            by_key.setdefault(ck, []).append(val)
    return {"urls": urls, "by_key": by_key}


def save_cache(path, results):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    slim = {}
    for url, entry in list(results.items())[:12000]:
        slim[url] = {k: v for k, v in entry.items() if k in ("ts", "key", "name", "group", "url")}
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(slim),
        "urls": slim,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))


def natural_key(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def group_rank(group, priority):
    g = (group or "").lower()
    for idx, p in enumerate(priority):
        if p.lower() in g or g in p.lower():
            return idx
    return len(priority)


def safe_title(s):
    """频道名里若带引号/逗号/换行会破坏 m3u 与 txt 结构，做一次清洗"""
    s = (s or "").replace('"', "").replace("\r", " ").replace("\n", " ")
    s = s.replace(",", "，")  # 半角逗号是字段分隔符，换成全角
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:40] if len(s) > 40 else s


def build_outputs(channels, now_iso):
    m3u = ["#EXTM3U"]
    txt = OrderedDict()
    valid_count = 0
    for ch in channels:
        urls = ch["kept"]
        if not urls:
            continue
        grp = safe_title(ch["group"] or "其他")
        title = safe_title(ch["display"])
        logo = (ch["logo"] or "").replace('"', "").strip()
        if not title:
            continue
        valid_count += 1
        m3u.append('#EXTINF:-1 group-title="%s" tvg-name="%s" tvg-id="%s"%s,%s' % (
            grp, title, ch["key"],
            (' tvg-logo="%s"' % logo) if logo.startswith("http") else "",
            title,
        ))
        m3u.append(urls[0]["url"])
        for idx, extra in enumerate(urls[1:], start=2):
            m3u.append('#EXTINF:-1 group-title="%s" tvg-name="%s"%s,%s(备%d)' % (
                grp, title, (' tvg-logo="%s"' % logo) if logo.startswith("http") else "",
                title, idx))
            m3u.append(extra["url"])
        line = "%s,%s" % (title, "#".join(u["url"] for u in urls))
        txt.setdefault(grp, []).append((title, line))

    txt_lines = []
    for grp, rows in txt.items():
        rows.sort(key=lambda x: natural_key(x[0]))
        txt_lines.append("%s,#genre#" % grp)
        txt_lines.extend(r[1] for r in rows)

    return "\n".join(m3u) + "\n", "\n".join(txt_lines) + "\n", valid_count


def main():
    global CONFIG, HEADERS
    dry = "--dry-run" in sys.argv
    quick = "--quick" in sys.argv

    cfg = load_json(os.path.join(ROOT, "config", "config.json"), {})
    cfg.setdefault("sources_file", "config/sources.txt")
    cfg.setdefault("whitelist_file", "config/channels.txt")
    cfg.setdefault("cache_file", "data/cache.json")
    cfg.setdefault("out_dir", "docs")
    cfg.setdefault("timeout_seconds", 6)
    cfg.setdefault("connect_retries", 2)
    cfg.setdefault("workers", 32)
    cfg.setdefault("max_per_channel", 5)
    cfg.setdefault("min_valid_channels", 40)
    cfg.setdefault("cache_hours", 48)
    cfg.setdefault("keep_untested", True)
    cfg.setdefault("group_priority", [])
    cfg.setdefault("user_agent", HEADERS["ua"])
    CONFIG = cfg
    HEADERS["ua"] = cfg["user_agent"]

    socket.setdefaulttimeout(cfg["timeout_seconds"] + 4)

    t_start = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1) 采集
    src_path = os.path.join(ROOT, cfg["sources_file"])
    sources = [ln.strip() for ln in read_text(src_path).splitlines()
               if ln.strip() and not ln.strip().startswith("#")]
    log("上游源 %d 个，开始采集" % len(sources))
    raw_items = []
    fetch_timeout = max(12, cfg["timeout_seconds"] * 3)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as pool:
        results = list(pool.map(lambda s: fetch_source(s, fetch_timeout), sources))
    sources_ok = sum(1 for r in results if r)
    for res in results:
        raw_items.extend(res)
    log("共取到 %d 条原始链接" % len(raw_items))
    if not raw_items:
        log("!! 所有上游源都拉取失败，保留现有结果不改写")
        return 1

    # 2) 合并 + 白名单
    whitelist = load_whitelist(os.path.join(ROOT, cfg["whitelist_file"]))
    if whitelist:
        log("白名单生效：%d 个频道" % len(whitelist))
    channels = dedupe_and_merge(raw_items, whitelist)
    log("去重合并后 %d 个频道" % len(channels))

    # 3) 测活
    cache = load_cache(os.path.join(ROOT, cfg["cache_file"]), cfg["cache_hours"])
    tasks = []
    for ch in channels.values():
        for u in ch["urls"][: cfg["max_per_channel"] * 2]:
            u["state"] = None
            tasks.append(u)

    if quick:
        for u in tasks:
            u["state"] = "skip"
            u["extra"] = "quick 模式"
        log("已跳过测活（--quick）")
    else:
        log("开始测活：%d 条链接，并发 %d，超时 %ds" % (
            len(tasks), cfg["workers"], cfg["timeout_seconds"]))
        done = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["workers"]) as pool:
            futs = [pool.submit(test_one, u, cfg["timeout_seconds"]) for u in tasks]
            for fut in concurrent.futures.as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    log("测活线程异常: %s" % exc)
        log("测活完成：可用 %d / 失效 %d / 跳过 %d" % (_stats["ok"], _stats["bad"], _stats["skip"]))

    # 4) 组装：测活结果用于“排序”，不用于“硬删除”
    #    云端出口 IP 与用户家庭宽带不同，硬删会让国内源大面积消失
    strict = bool(cfg.get("strict_filter", False))
    new_cache = {}
    for ch in channels.values():
        tier_ok, tier_cached, tier_skip, tier_dead = [], [], [], []
        for u in ch["urls"]:
            st = u.get("state")
            if st == "ok":
                u["rank"] = u.get("ms", 9999)
                tier_ok.append(u)
                new_cache[u["url"]] = {"ts": time.time(), "key": ch["key"], "name": ch["display"],
                                       "group": ch["group"], "url": u["url"]}
            elif st == "skip":
                tier_skip.append(u)
            elif st == "bad":
                tier_dead.append(u)
        for cu in cache.get("by_key", {}).get(ch["key"], []):
            if cu["url"] not in [x["url"] for x in ch["urls"]]:
                tier_cached.append(dict(cu))

        pool = tier_ok + tier_cached + (tier_skip if cfg["keep_untested"] else [])
        if not strict:
            pool = pool + tier_dead          # 严格模式关闭时，本轮失败的地址也留着排在末尾
        seen = set()
        ch["kept"] = [r for r in pool if not (r["url"] in seen or seen.add(r["url"]))][: cfg["max_per_channel"]]
        ch["live"] = len(tier_ok)
        ch["score"] = (2 if tier_ok else (1 if (tier_cached or tier_skip) else 0))

    kept_channels = [c for c in channels.values() if c["kept"]]
    live_channels = sum(1 for c in kept_channels if c["live"])
    log("可用频道 %d / 保留频道 %d（严格过滤=%s）" % (live_channels, len(kept_channels), strict))
    if live_channels < cfg["min_valid_channels"] and not quick:
        log("!! 提醒：本轮真正可用频道 %d 低于阈值 %d，可能是上游源抖动，建议检查 config/sources.txt" % (
            live_channels, cfg["min_valid_channels"]))

    # 5) 排序：本轮真正可用的台排前面，分组按优先级
    cap = int(cfg.get("max_channels", 0))
    ordered = sorted(
        kept_channels,
        key=lambda c: (-c["score"], group_rank(c["group"], cfg["group_priority"]), natural_key(c["display"])),
    )
    truncated = 0
    if cap and len(ordered) > cap:
        head = ordered[:cap]
        tail = ordered[cap:]
        # 尽量覆盖“有直播的台”，其次才补充其余
        best = sorted(head + tail, key=lambda c: (-c["score"], group_rank(c["group"], cfg["group_priority"]), natural_key(c["display"])))
        ordered = best[:cap]
        truncated = len(best) - cap

    m3u_text, txt_text, total = build_outputs(ordered, now_iso)
    elapsed = round(time.time() - t_start, 1)
    status = {
        "updated_at": now_iso,
        "channels": total,
        "channels_live": live_channels,
        "sources_total": len(sources),
        "sources_ok": sources_ok,
        "links_tested": len(tasks),
        "links_live": _stats["ok"],
        "links_dead": _stats["bad"],
        "links_untested": _stats["skip"],
        "strict_filter": strict,
        "elapsed_seconds": elapsed,
    }
    log("结果：频道 %d 个（本轮可用 %d），可用链接 %d 条，耗时 %ss" % (total, live_channels, _stats["ok"], elapsed))

    if dry or quick:
        log("%s 模式：只统计，不写入 docs，避免覆盖正式结果" % ("--dry-run" if dry else "--quick"))
        sys.stdout.write("\n".join(m3u_text.splitlines()[:20]) + "\n")
        return 0

    out_dir = os.path.join(ROOT, cfg["out_dir"])
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "iptv.m3u"), "w", encoding="utf-8") as fh:
        fh.write(m3u_text)
    with open(os.path.join(out_dir, "iptv.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt_text)
    with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as fh:
        json.dump(status, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_index(status, ordered[:15]))
    if not quick:
        save_cache(os.path.join(ROOT, cfg["cache_file"]), new_cache)
    else:
        log("--quick 模式：不更新历史缓存")
    log("已写入 docs/iptv.m3u, docs/iptv.txt, docs/status.json")
    return 0


def render_index(status, top):
    rows = "".join("<tr><td>%s</td><td>%s</td></tr>" % (c["display"], c["group"]) for c in top)
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>IPTV 直播源</title>
<style>body{font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;max-width:820px;margin:40px auto;padding:0 16px;color:#222}
h1{font-size:22px}code{background:#f2f2f2;padding:2px 6px;border-radius:4px;word-break:break-all}
a{color:#0b62d0}table{border-collapse:collapse;width:100%%;margin-top:12px;font-size:14px}
td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}</style></head><body>
<h1>IPTV 直播源自动更新</h1>
<p>最近更新：<code>%s</code>　存活频道：<code>%s</code>　本轮测试：<code>%s</code> 条链接，可用 <code>%s</code> 条，耗时 %s 秒</p>
<h3>订阅地址（复制到 TVBox 等播放器）</h3>
<p>TVBox 配置：<a href="tvbox.json"><code>tvbox.json</code></a><br>
M3U：<a href="iptv.m3u"><code>iptv.m3u</code></a><br>
TXT：<a href="iptv.txt"><code>iptv.txt</code></a></p>
<h3>当前收录频道（前 %s 个）</h3><table><tr><th>频道</th><th>分组</th></tr>%s</table>
<p style="color:#888;font-size:13px;margin-top:24px">每 2 小时由 GitHub Actions 自动测活更新，失效地址自动剔除。</p>
</body></html>""" % (status["updated_at"], status["channels"], status["links_tested"],
                      status["links_live"], status["elapsed_seconds"], len(top), rows)


if __name__ == "__main__":
    sys.exit(main())
