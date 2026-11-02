#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键把本项目推送到 GitHub（不需要本机装 Git，只用 Python 标准库 + GitHub REST API）。

用法（在 PowerShell 里）：
    cd D:\\Documents\\qwen-agent\\AfnqLDn9QF\\default\\IPTV
    $env:GH_TOKEN = "ghp_xxx 或 github_pat_xxx"
    python tools\\init_repo.py

可选环境变量：
    GH_OWNER   默认 DK1100DK
    GH_REPO    默认 IPTV
    GH_BRANCH  默认 main

token 权限要求：Fine-grained PAT → 目标仓库 Contents: Read and write
              经典 PAT        → repo 勾选
脚本会：确保仓库存在（Public）→ 逐个上传/覆盖文件 → 触发一次 Actions 运行。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("GH_API", "https://api.github.com")
OWNER = os.environ.get("GH_OWNER", "DK1100DK")
REPO = os.environ.get("GH_REPO", "iptv")
BRANCH = os.environ.get("GH_BRANCH", "main")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 必须上传的文件；目录前缀写全，含隐藏目录 .github
UPLOAD_FILES = [
    "README.md",
    "tvbox.json",
    ".github/workflows/update.yml",
    "config/sources.txt",
    "config/channels.txt",
    "config/config.json",
    "scripts/update.py",
    "tools/init_repo.py",
    "data/cache.json",
    "docs/.nojekyll",
]

# 可选：本地跑过 python scripts/update.py 才会生成，有就一起传，链接推送后立即可看
OPTIONAL_FILES = [
    "docs/iptv.m3u",
    "docs/iptv.txt",
    "docs/status.json",
    "docs/index.html",
]

KEEP_LIVE = ("docs/iptv.m3u", "docs/iptv.txt", "docs/status.json", "docs/index.html")


def read_file_first_line(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.readline().strip()
    except Exception:  # noqa: BLE001
        return ""


def api(method, path, body=None, token=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "iptv-init")
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, {"message": raw.decode("utf-8", "replace")[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"message": "%s: %s" % (type(exc).__name__, exc)}


def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        tf = os.environ.get("GH_TOKEN_FILE")
        if tf and os.path.isfile(tf):
            token = read_file_first_line(tf)
    if not token:
        print("请先设置环境变量 GH_TOKEN（GitHub 个人访问令牌）。")
        print("  PowerShell 示例： $env:GH_TOKEN = \"你的token\"; python tools\\init_repo.py")
        print("  或指向文件：     $env:GH_TOKEN_FILE = \"C:\\path\\to\\token.txt\"")
        return 2

    st, me = api("GET", "/user", token=token)
    if st != 200:
        print("token 校验失败（HTTP %s）：%s" % (st, me.get("message")))
        return 1
    login = me.get("login", "")
    print("当前 token 所属账号：%s" % login)
    if login.lower() != OWNER.lower():
        print("注意：token 账号是 %s，但目标仓库写的是 %s/%s" % (login, OWNER, REPO))

    # 1) 确保仓库存在
    st, info = api("GET", "/repos/%s/%s" % (OWNER, REPO), token=token)
    if st == 404:
        st2, created = api("POST", "/user/repos", {
            "name": REPO,
            "description": "IPTV 直播源自动采集 + 每2小时测活更新，输出 TVBox 订阅链接",
            "private": False,
            "auto_init": True,
        }, token=token)
        if st2 not in (201, 200):
            print("创建仓库失败（HTTP %s）：%s" % (st2, created.get("message")))
            return 1
        print("已创建 Public 仓库 %s/%s" % (OWNER, REPO))
    elif st != 200:
        print("读取仓库失败（HTTP %s）：%s" % (st, info.get("message")))
        return 1
    else:
        print("仓库已存在：%s（forks=%s，可见性=%s）" % (
            info.get("full_name"), info.get("forks_count"),
            "Private" if info.get("private") else "Public"))

    default_branch = info.get("default_branch") or BRANCH
    branch = default_branch if default_branch in (BRANCH, "master") else BRANCH

    # 2) 上传文件
    ok, fail = 0, []
    todo = [(rel, False) for rel in UPLOAD_FILES] + [(rel, True) for rel in OPTIONAL_FILES]
    for rel, optional in todo:
        abs_path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(abs_path):
            if optional:
                print("  [跳过] %s（本地未生成，Actions 首轮会自动创建）" % rel)
                continue
            fail.append("%s (本地文件不存在)" % rel)
            continue
        with open(abs_path, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        body = {"message": "init: %s" % rel, "content": content, "branch": branch}
        st_get, cur = api("GET", "/repos/%s/%s/contents/%s?ref=%s" % (OWNER, REPO, rel, branch), token=token)
        if st_get == 200 and cur.get("sha"):
            body["sha"] = cur["sha"]
        st_put, res = api("PUT", "/repos/%s/%s/contents/%s" % (OWNER, REPO, rel), body, token=token)
        if st_put in (200, 201):
            ok += 1
            print("  [OK] %s" % rel)
        else:
            fail.append("%s -> HTTP %s %s" % (rel, st_put, res.get("message", "")[:120]))
            print("  [FAIL] %s : %s" % (rel, res.get("message", ""))[:160])

    # 3) 触发 Actions
    st, res = api("POST", "/repos/%s/%s/actions/workflows/update.yml/dispatches" % (OWNER, REPO),
                  {"ref": branch}, token=token)
    print("触发首次运行：%s (HTTP %s)" % ("成功" if st == 204 else "未成功，推送本身也会自动触发", st))

    # 4) 启用 GitHub Pages（来源 main 分支 / docs 文件夹），失败不影响主流程
    enable_pages(token, branch)

    print("\n上传完成：%d 个文件成功" % ok)
    if fail:
        print("失败项：")
        for f in fail:
            print("  - " + f)

    pages = ("https://%s.github.io/%s/tvbox.json" % (OWNER.lower(), REPO))
    print("\n下一步：")
    print("1) 打开 https://github.com/%s/%s/actions 查看 “IPTV 自动更新” 是否跑起来" % (OWNER, REPO))
    print("2) 首次进 Actions 若提示需确认，点 “I understand my workflows, go ahead and enable them”")
    print("3) Settings → Actions → General → Workflow permissions 改为 Read and write permissions")
    print("4) 跑完后 TVBox 配置地址填：https://raw.githubusercontent.com/%s/%s/%s/tvbox.json" % (OWNER, REPO, branch))
    print("   备用线路：%s" % pages)
    return 0 if not fail else 1


def enable_pages(token, branch):
    """开启 Pages 并把 build_type 设为 workflow 之外的简单方式；已开启则跳过。"""
    st, res = api("GET", "/repos/%s/%s/pages" % (OWNER, REPO), token=token)
    if st == 200 and res.get("status") not in (None, "built_in_error"):
        print("GitHub Pages 已开启：%s" % res.get("url"))
        return
    st, res = api("POST", "/repos/%s/%s/pages" % (OWNER, REPO),
                  {"source": {"branch": branch, "root": "/docs"}}, token=token)
    if st in (201, 200):
        print("GitHub Pages 已启用（%s 分支 /docs）" % branch)
    elif st == 422:
        print("Pages 启用返回 422：可能仓库无可见性设置或已在排队，稍后到 Settings→Pages 手动确认")
    else:
        print("Pages 启用未成功(HTTP %s)，可稍后在 Settings→Pages 手动开启" % st)


if __name__ == "__main__":
    sys.exit(main())
