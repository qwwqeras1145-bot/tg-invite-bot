# -*- coding: utf-8 -*-
"""
禁漫天堂 (JMComic) 下载模块 —— Telegram 版
==========================================
移植自 QQ 版 jmqq-download-bot（作者：小鱼儿），TG 版差异：
  * 不加密打包（按需求去掉 AES-256 加密）
  * 修复「页码太多下载失败」：
      1) Telegram 单文件上限 50MB → 自动分卷压缩（每卷 ≤45MB）+ 逐卷发送
      2) 下载并发 30 → 8（大本子不再把图源打挂）
      3) 断点续传 + 自动清理 <10KB 的半截文件 + 整本 3 次重试
  * 命令与 QQ 版对齐（/jm ...）

依赖：vendor_jm/（免安装的 jmcomic + 依赖，Linux 版 wheels）
"""

import os
import re
import sys
import time
import json
import random
import shutil
import zipfile
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor_jm")
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)

JM_DIR = os.path.join(HERE, "data", "jm")
DL_DIR = os.path.join(JM_DIR, "albums")
ZIP_DIR = os.path.join(JM_DIR, "zips")
VOL_MAX = 45 * 1024 * 1024          # Telegram Bot API 单文件 50MB，留余量
CONCURRENCY = 8                     # 下载并发（QQ 版默认 30，容易把图源打挂）
RETRIES = 3
PROGRESS_INTERVAL = 15              # 进度播报间隔（秒）

VERSION = "tg-1.0.0"

_TASKS = {}                          # code -> {"state":..., "files":n, "total":n, "err":...}
_LOCK = threading.Lock()
INDEX_PATH = os.path.join(JM_DIR, "index.json")
SETTINGS_PATH = os.path.join(JM_DIR, "settings.json")

# 自动清理模式：off=关闭 immediate=发送成功后立刻删 30m/5h/1d=按时间
AUTO_MODES = {"off": 0, "immediate": 0, "30m": 30 * 60, "5h": 5 * 3600, "1d": 24 * 3600}
AUTO_MODE_CN = {"off": "关闭", "immediate": "发送成功后立即删除",
                "30m": "30 分钟后删除", "5h": "5 小时后删除", "1d": "1 天后删除"}


def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(s):
    _ensure()
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def get_auto_delete():
    return _load_settings().get("auto_delete", "off")


def set_auto_delete(mode):
    mode = str(mode or "").strip().lower()
    if mode not in AUTO_MODES:
        raise ValueError("模式必须是 off / immediate / 30m / 5h / 1d")
    s = _load_settings()
    s["auto_delete"] = mode
    _save_settings(s)
    return mode


def cleanup_expired(now=None):
    """按设定的自动清理模式删除过期下载。返回删除本数。"""
    mode = get_auto_delete()
    ttl = AUTO_MODES.get(mode)
    if not ttl:                     # off / 未配置
        return 0
    if mode == "immediate":         # 立即模式在发送后即时删除，这里不重复处理
        return 0
    now = now or int(time.time())
    removed = 0
    for it in list_downloaded():
        t = it.get("time") or 0
        if not t:
            try:
                t = int(os.path.getmtime(os.path.join(DL_DIR, it["code"])))
            except OSError:
                t = 0
        if t and now - t >= ttl:
            try:
                remove_download(it["code"])
                removed += 1
                print(f"[jm] 自动清理：已删除 JM{it['code']}（{AUTO_MODE_CN.get(mode, mode)}）", flush=True)
            except ValueError:
                pass
    return removed


def _load_index():
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(idx):
    _ensure()
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def remember(code, title, pages):
    idx = _load_index()
    idx[str(code)] = {"title": title or "", "pages": int(pages or 0), "time": int(time.time())}
    _save_index(idx)


def list_downloaded():
    """已下载清单（网页管理页用）：[{code,title,pages,files,size,zips,time}]"""
    _ensure()
    idx = _load_index()
    items = []
    if os.path.isdir(DL_DIR):
        for name in sorted(os.listdir(DL_DIR), reverse=True):
            d = os.path.join(DL_DIR, name)
            if not os.path.isdir(d):
                continue
            files = _count_images(d)
            if not files:
                continue
            # 递归统计（分章节目录后图片在子目录里，平铺扫描会算成 0）
            size = sum(os.path.getsize(p) for p, _rel in _iter_images(d))
            zips = []
            if os.path.isdir(ZIP_DIR):
                zips = [f for f in os.listdir(ZIP_DIR)
                        if f.startswith(f"JM{name}") and f.endswith(".zip")]
                for f in zips:
                    try:
                        size += os.path.getsize(os.path.join(ZIP_DIR, f))
                    except OSError:
                        pass
            items.append({
                "code": name,
                "title": (idx.get(name) or {}).get("title", ""),
                "pages": (idx.get(name) or {}).get("pages", 0),
                "files": files,
                "size": size,
                "zips": len(zips),
                "time": (idx.get(name) or {}).get("time", 0),
            })
    return items


def remove_download(code):
    """删除一本已下载的漫画（目录 + 所有分卷 zip + 索引）。"""
    code = normalize_code(code)
    removed = []
    d = os.path.join(DL_DIR, code)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d)
    if os.path.isdir(ZIP_DIR):
        for f in os.listdir(ZIP_DIR):
            if f.startswith(f"JM{code}") and f.endswith(".zip"):
                try:
                    os.remove(os.path.join(ZIP_DIR, f))
                    removed.append(f)
                except OSError:
                    pass
    idx = _load_index()
    idx.pop(str(code), None)
    _save_index(idx)
    if not removed:
        raise ValueError(f"本地没有 JM{code} 的下载记录")
    return removed


def disk_used():
    """已下载占用的磁盘空间（字节）。"""
    total = 0
    if os.path.isdir(DL_DIR):
        for root, _dirs, files in os.walk(DL_DIR):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    if os.path.isdir(ZIP_DIR):
        for f in os.listdir(ZIP_DIR):
            try:
                total += os.path.getsize(os.path.join(ZIP_DIR, f))
            except OSError:
                pass
    return total


def _ensure():
    os.makedirs(DL_DIR, exist_ok=True)
    os.makedirs(ZIP_DIR, exist_ok=True)


def normalize_code(raw):
    s = str(raw).strip()
    m = re.search(r"(?:album|photos/index/|photo/)(\d{4,})", s, re.I) or \
        re.search(r"JM\s*(\d+)", s, re.I) or re.search(r"\b(\d{4,})\b", s)
    if m:
        return m.group(1)
    raise ValueError(f"无法识别漫画码：{raw}")


_BOM_PATCHED = False


def _ensure_patch():
    """图源偶尔在 JSON 响应开头带 UTF-8 BOM（\ufeff），jmcomic 会误判"不是JSON"。

    这里给底层 HTTP 库打补丁：.text / .json 都自动剥掉 BOM，四个域名不再全军覆没。
    """
    global _BOM_PATCHED
    if _BOM_PATCHED:
        return
    _BOM_PATCHED = True
    try:  # jmcomic 默认走 curl_cffi
        from curl_cffi.requests import Response as CResp
        _orig_text = CResp.__dict__.get("text")
        _orig_json = CResp.json

        def _text(self):
            t = _orig_text.__get__(self, CResp) if _orig_text else self.content.decode("utf-8", "replace")
            return t[1:] if t.startswith("\ufeff") else t

        def _json(self, *a, **k):
            try:
                return _orig_json(self, *a, **k)
            except Exception:
                import json as _j
                data = self.content
                if data[:3] == b"\xef\xbb\xbf":
                    data = data[3:]
                return _j.loads(data)

        CResp.text = property(_text)
        CResp.json = _json
    except Exception:
        pass
    try:  # 普通 requests 也顺手打上（random_album 用）
        import requests as _r
        _rj = _r.models.Response.json

        def _json2(self, *a, **k):
            try:
                return _rj(self, *a, **k)
            except Exception:
                import json as _j
                data = self.content
                if data[:3] == b"\xef\xbb\xbf":
                    data = data[3:]
                return _j.loads(data)

        _r.models.Response.json = _json2
    except Exception:
        pass


def _jmcomic():
    if "jmcomic" not in sys.modules:
        import jmcomic
    return sys.modules["jmcomic"]


def _option(concurrency=CONCURRENCY):
    from jmcomic import JmOption, DirRule
    opt = JmOption.default()
    try:
        # 关键修复：多章节本子（上+下）各章节图片文件名相同（00001.webp…），
        # 放同一目录会导致第二章被"文件已存在"全部跳过（只下一半的根因）。
        # 按 专辑/章节 分目录，彻底消除文件名冲突。
        opt.dir_rule = DirRule("Bd/Aid/Pid", base_dir=DL_DIR)
    except Exception:
        pass
    try:
        opt.download.threading.image = int(concurrency)
        opt.download.threading.photo = 1
    except Exception:
        pass
    return opt


def _client():
    _ensure_patch()
    return _option().new_jm_client()


# ---------------- 查询 / 搜索 / 排行 ----------------

def about(code):
    code = normalize_code(code)
    album = _client().get_album_detail(code)
    tags = []
    try:
        for t in album.tags:
            tags.append(getattr(t, "tag", str(t)))
    except Exception:
        pass
    return {
        "id": str(album.id),
        "title": getattr(album, "title", ""),
        "author": getattr(album, "author", ""),
        "pages": getattr(album, "page_count", 0),
        "tags": tags,
    }


def search(kw, page=1):
    items = _client().search_site(kw, page=int(page))
    out = []
    for it in items:
        if isinstance(it, tuple) and len(it) >= 2:
            aid, payload = it[0], it[1]
            if isinstance(payload, dict):
                out.append({"id": str(aid), "title": payload.get("name", "")})
            else:
                out.append({"id": str(aid), "title": str(payload)})
        else:
            out.append({"id": str(getattr(it, "id", "")), "title": getattr(it, "title", "")})
    return out


def top(kind="day", page=1):
    c = _client()
    fn = {"day": c.day_ranking, "week": c.week_ranking, "month": c.month_ranking}.get(kind)
    if fn is None:
        raise ValueError(f"未知榜单: {kind}")
    out = []
    for it in fn(int(page)):
        if isinstance(it, tuple) and len(it) >= 2:
            aid, payload = it[0], it[1]
            title = payload.get("name", "") if isinstance(payload, dict) else str(payload)
            out.append({"id": str(aid), "title": title})
        else:
            out.append({"id": str(getattr(it, "id", "")), "title": getattr(it, "title", "")})
    return out


def random_album():
    """全站随机挑一本（走官方 search 空关键词接口 + AES 解密）"""
    import hashlib
    import base64
    import requests
    from Crypto.Cipher import AES
    secret = "185Hcomic3PAPP7R"
    ua = ("Mozilla/5.0 (Linux; Android 12; V2366GA Build/V417IR; wv) "
          "AppleWebKit/537.36")
    ts = int(time.time())
    headers = {
        "User-Agent": ua,
        "Tokenparam": f"{ts},2.0.30",
        "Token": hashlib.md5(f"{ts}{secret}".encode()).hexdigest(),
        "x-requested-with": "com.a7m3p9xv.t6qk2z8.app",
        "Accept": "*/*",
    }
    page = random.randint(1, 200)
    r = requests.get("https://www.cdngwc.cc/search?search_query=&page=%d" % page,
                     headers=headers, timeout=30)
    j = r.json()
    if not j.get("data"):
        raise RuntimeError("随机接口返回异常")
    key = hashlib.md5(f"{ts}{secret}".encode()).hexdigest().encode()
    pad = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(j["data"]))
    d = json.loads(pad[:-pad[-1]].decode())
    content = d.get("content") or []
    if not content:
        raise RuntimeError("随机列表为空")
    pick = random.choice(content)
    return str(pick["id"]), pick["name"]


# ---------------- 下载 ----------------

def _iter_images(d):
    """递归列出目录下所有图片（按相对路径自然排序）。"""
    out = []
    if not os.path.isdir(d):
        return out
    for root, _dirs, names in os.walk(d):
        for n in names:
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                p = os.path.join(root, n)
                out.append((p, os.path.relpath(p, d)))
    out.sort(key=lambda x: (len(x[1]), x[1]))
    return out


def _count_images(d):
    return len(_iter_images(d))


def _clean_half(album_dir):
    removed = 0
    if os.path.isdir(album_dir):
        for p, _rel in _iter_images(album_dir):
            if os.path.getsize(p) < 10 * 1024:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
    return removed


def _migrate_flat(album_dir, aid):
    """老目录是平铺的（无章节子目录）：把图片挪进 <aid>/ 子目录，适配新的分章节布局。"""
    flat = [f for f in os.listdir(album_dir)
            if os.path.isfile(os.path.join(album_dir, f))
            and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))]
    if not flat:
        return
    sub = os.path.join(album_dir, str(aid))
    os.makedirs(sub, exist_ok=True)
    moved = 0
    for f in flat:
        try:
            os.replace(os.path.join(album_dir, f), os.path.join(sub, f))
            moved += 1
        except OSError:
            pass
    if moved:
        log_msg = f"已迁移 {moved} 张旧版平铺图片到章节目录"
        print(f"[jm] {log_msg}", flush=True)


def make_volumes(album_dir, code, vol_max=VOL_MAX):
    """把图片目录（含章节子目录）打包成 ≤vol_max 的分卷 zip（返回卷路径列表）。"""
    _ensure()
    files = _iter_images(album_dir)
    if not files:
        raise RuntimeError("没有可打包的图片")
    vols, cur, cur_size, idx = [], None, 0, 0
    for p, arc in files:
        if cur is not None and cur_size + os.path.getsize(p) > vol_max:
            cur.close()
            cur = None
        if cur is None:
            idx += 1
            name_zip = f"JM{code}.zip" if idx == 1 else f"JM{code}.part{idx}.zip"
            cur = zipfile.ZipFile(os.path.join(ZIP_DIR, name_zip), "w", zipfile.ZIP_STORED)
            vols.append(cur.filename)
            cur_size = 0
        cur.write(p, arc.replace("\\", "/"))
        cur_size += os.path.getsize(p)
    if cur is not None:
        cur.close()
    return vols


def download(code, title="", on_doc=None, on_result=None):
    """静默下载：过程中不发任何消息，只在结束时回调一次结果。

    on_doc(filename, data, caption)：每卷压缩包发一次（由机器人发送）
    on_result(dict)：{"ok":bool, "code", "files", "expected", "vols", "error"}
    失败也走 on_result（ok=False），不会静默吞掉。
    """
    code = normalize_code(code)
    _ensure()
    _ensure_patch()
    album_dir = os.path.join(DL_DIR, code)
    with _LOCK:
        _TASKS[code] = {"state": "downloading", "files": 0, "total": 0, "err": None}
    try:
        if os.path.isdir(album_dir):
            _clean_half(album_dir)
            _migrate_flat(album_dir, code)   # 旧平铺目录 → 分章节目录
        # 预期页数：先查一次详情（用于缺页补全判断）
        expected = 0
        try:
            expected = int(_client().get_album_detail(code).page_count or 0)
        except Exception:
            expected = 0
        # 自适应补全：8→4→2→1 并发逐轮补缺，最多 15 轮；
        # 连续 3 轮零增长 = 图源真缺页，自动收手不再空转
        plan = [(8, 2), (8, 4), (4, 6), (2, 10), (1, 15)] + [(1, 20)] * 10
        last, no_progress = -1, 0
        for idx, (conc, wait) in enumerate(plan):
            try:
                _jmcomic().download_album(int(code), option=_option(conc), check_exception=False)
            except Exception:
                pass
            n = _count_images(album_dir)
            if expected and n >= expected:
                break
            if n == last:
                no_progress += 1
                if no_progress >= 3:
                    break
            else:
                no_progress = 0
            last = n
            if idx < len(plan) - 1:
                time.sleep(wait)
        files = _count_images(album_dir)
        if not files:
            raise RuntimeError("未下载到任何图片（图源波动，稍后再试 /jm dl %s）" % code)
        remember(code, title, expected or files)
        vols = make_volumes(album_dir, code)
        for i, v in enumerate(vols, 1):
            size_mb = round(os.path.getsize(v) / 1024 / 1024, 1)
            with open(v, "rb") as f:
                data = f.read()
            if on_doc:
                on_doc(os.path.basename(v), data,
                       f"JM{code} 第 {i}/{len(vols)} 卷（{size_mb} MB）")
            time.sleep(0.3)
        with _LOCK:
            _TASKS[code] = {"state": "done", "files": files, "total": files, "err": None}
        if on_result:
            on_result({"ok": True, "code": code, "files": files,
                       "expected": expected, "vols": vols, "error": None})
        # 立即删除模式：发送成功后当场清掉本地文件，不占磁盘
        if get_auto_delete() == "immediate":
            try:
                remove_download(code)
            except ValueError:
                pass
    except Exception as e:
        traceback.print_exc()
        files = _count_images(album_dir)
        with _LOCK:
            _TASKS[code] = {"state": "error", "files": files, "total": 0, "err": str(e)[:200]}
        if on_result:
            on_result({"ok": False, "code": code, "files": files,
                       "expected": 0, "vols": [], "error": str(e)[:300]})


def task_state(code):
    code = normalize_code(code)
    with _LOCK:
        return dict(_TASKS.get(code, {"state": "none", "files": 0, "total": 0, "err": None}))


def help_text():
    return (
        f"📕 <b>禁漫下载</b>（TG 版 v{VERSION}，不加密 · 自动分卷）\n\n"
        "指令（中英文都行）：\n"
        "  /jm &lt;漫画码&gt; — 查询信息（同 /jm 查询/详情）\n"
        "  /jm dl &lt;漫画码&gt; — 下载（同 /jm 下载）\n"
        "  /jm search &lt;关键词&gt; [页码] — 搜索（同 /jm 搜索）\n"
        "  /jm top [日榜|周榜|月榜] [页码] — 排行榜（同 /jm 排行）\n"
        "  /jm random — 全站随机（同 /jm 随机）\n"
        "  /jm progress &lt;漫画码&gt; — 下载进度（同 /jm 进度）\n"
        "  /jm version — 版本（同 /jm 版本）\n"
        "示例：/jm dl 515320 或 /jm 下载 515320"
    )
