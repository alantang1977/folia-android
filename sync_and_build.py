#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
folia-android 上游同步 & APK 自动构建发布脚本（CI 友好版）
================================================

在 GitHub Actions 中运行时，直接使用已 checkout 的工作区，不重复克隆。
本地运行时，自动克隆仓库到临时目录。

用法：
  export GITHUB_TOKEN=ghp_xxxxx
  python3 sync_and_build.py              # 同步+构建+发布
  python3 sync_and_build.py --build-only # 只构建当前代码，不同步
  python3 sync_and_build.py --dry-run    # 只打印计划
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# 配置区
# ============================================================================

UPSTREAM_REPO = "https://github.com/chthollyphile/folia-major.git"
UPSTREAM_BRANCH = "main"

TARGET_OWNER = "alantang1977"
TARGET_REPO = "folia-android"
TARGET_BRANCH = "main"

# 从上游同步的目录
SYNC_DIRS = [
    "src", "shared", "public", "mods", "assets", "skills",
    "dev", "docs", "build", "packaging", "sync-server",
    "native", "test", "worker", "api", "api-ts", "img",
    "electron",
]

# 从上游同步的顶层文件
SYNC_FILES = [
    "vite.config.ts", "tsconfig.json", "tailwind.config.js", "postcss.config.js",
    "index.html", "metadata.json", ".env.example", ".gitignore",
    ".editorconfig", ".npmrc", ".nvmrc",
    "playwright.config.ts", "playwright.probe.config.ts", "vitest.config.ts",
    "vercel.json", "wrangler.jsonc",
    "AGENTS.md", "CLAUDE.md", "README.en.md",
    "CONTRIBUTING.en.md", "CONTRIBUTING.md", "CONTRIBUTORS.md",
    "LICENSE", "package-lock.json",
]

# 绝对保留，不用上游覆盖
PRESERVE_PATHS = [
    "android",
    "capacitor.config.json",
    "README.md",
    "mu.txt", "qr_base64.txt", "qr_dataurl.txt",
    ".github",
    ".gemini",
    "sync_and_build.py",
]

# 注入到 package.json 的 Android 依赖
ANDROID_DEPS = {
    "@capacitor/android": "^8.5.3",
    "@capacitor/cli": "^8.5.3",
    "@capacitor/core": "^8.5.3",
    "@capacitor/filesystem": "^8.1.5",
    "@capacitor/splash-screen": "^8.0.2",
    "@capacitor/status-bar": "^8.0.4",
    "crypto-es": "^3.1.3",
    "qrcode": "^1.5.4",
}
ANDROID_DEV_DEPS = {
    "@types/qrcode": "^1.5.6",
}

# ============================================================================
# 工具函数
# ============================================================================

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd, cwd=".", env=None, check=True, capture=False):
    log(f"$ {' '.join(str(c) for c in cmd)}  (cwd={cwd})")
    merged = os.environ.copy()
    if env:
        merged.update(env)
    if capture:
        r = subprocess.run(cmd, cwd=str(cwd), env=merged,
                           capture_output=True, text=True)
    else:
        r = subprocess.run(cmd, cwd=str(cwd), env=merged)
    if check and r.returncode != 0:
        log(f"命令失败 exit={r.returncode}: {' '.join(str(c) for c in cmd)}", "ERROR")
        if capture:
            log(f"stdout: {r.stdout[-2000:]}", "ERROR")
            log(f"stderr: {r.stderr[-2000:]}", "ERROR")
        sys.exit(1)
    return r


def gh_api(method, path, token, body=None):
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "folia-android-sync",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode(errors="replace")
        log(f"GitHub API {method} {path} → {e.code}: {err}", "ERROR")
        raise


# ============================================================================
# 第一步：确定工作目录 & 获取上游代码
# ============================================================================

def setup_workdir():
    """
    返回 (target_dir, upstream_dir)。
    CI 环境（GITHUB_WORKSPACE 存在）直接用当前工作区作为 target_dir。
    本地环境克隆到临时目录。
    """
    ci_workspace = os.environ.get("GITHUB_WORKSPACE", "")
    if ci_workspace:
        target_dir = Path(ci_workspace)
        log(f"CI 模式，工作区: {target_dir}")
    else:
        # 本地模式：克隆目标仓库
        tmp = Path(tempfile.mkdtemp(prefix="folia_"))
        target_dir = tmp / "target"
        token = os.environ.get("GITHUB_TOKEN", "")
        target_url = f"https://{token}@github.com/{TARGET_OWNER}/{TARGET_REPO}.git"
        log(f"本地模式，克隆目标仓库到 {target_dir}")
        run(["git", "clone", "--depth", "50", "--branch", TARGET_BRANCH,
             target_url, str(target_dir)])

    # 上游仓库总是浅克隆到临时目录（只读）
    upstream_dir = Path(tempfile.mkdtemp(prefix="folia_upstream_"))
    log(f"克隆上游仓库到 {upstream_dir}")
    run(["git", "clone", "--depth", "1", "--branch", UPSTREAM_BRANCH,
         UPSTREAM_REPO, str(upstream_dir)])

    return target_dir, upstream_dir


# ============================================================================
# 第二步：同步文件
# ============================================================================

def is_preserved(rel):
    rel = rel.replace("\\", "/").lstrip("./")
    for p in PRESERVE_PATHS:
        p = p.replace("\\", "/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def sync_files_from_upstream(upstream_dir, target_dir, dry_run):
    """把上游文件复制到目标仓库。"""
    # 同步目录
    for dirname in SYNC_DIRS:
        src = upstream_dir / dirname
        dst = target_dir / dirname
        if not src.exists():
            continue
        if is_preserved(dirname):
            log(f"保留: {dirname}/")
            continue
        if dst.exists():
            log(f"删除旧: {dirname}/")
            if not dry_run:
                shutil.rmtree(dst)
        log(f"同步目录: {dirname}/")
        if not dry_run:
            shutil.copytree(src, dst, symlinks=True)

    # 同步顶层文件
    for fname in SYNC_FILES:
        src = upstream_dir / fname
        dst = target_dir / fname
        if not src.exists():
            continue
        if is_preserved(fname):
            log(f"保留: {fname}")
            continue
        log(f"同步文件: {fname}")
        if not dry_run:
            shutil.copy2(src, dst)


# ============================================================================
# 第三步：合并 package.json
# ============================================================================

def merge_package_json(upstream_dir, target_dir, dry_run):
    with open(upstream_dir / "package.json", encoding="utf-8") as f:
        pkg = json.load(f)

    pkg["name"] = "folia-android"

    deps = pkg.get("dependencies", {})
    deps.update(ANDROID_DEPS)
    pkg["dependencies"] = deps

    dev = pkg.get("devDependencies", {})
    dev.update(ANDROID_DEV_DEPS)
    pkg["devDependencies"] = dev

    log(f"package.json: version={pkg['version']}, deps={len(deps)}, devDeps={len(dev)}")
    if not dry_run:
        with open(target_dir / "package.json", "w", encoding="utf-8") as f:
            json.dump(pkg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return pkg


# ============================================================================
# 第四步：更新 Android 版本号
# ============================================================================

def version_to_code(v):
    nums = [int(x) for x in re.findall(r"\d+", v)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return max(nums[0] * 10000 + nums[1] * 100 + nums[2], 1)


def update_android_version(target_dir, version_name, dry_run):
    gradle = target_dir / "android" / "app" / "build.gradle"
    if not gradle.exists():
        log(f"未找到 {gradle}，跳过版本号更新", "WARN")
        return
    vc = version_to_code(version_name)
    log(f"Android 版本: versionName={version_name}, versionCode={vc}")
    content = gradle.read_text(encoding="utf-8")
    content = re.sub(r'versionName\s+["\'][^"\']*["\']',
                     f'versionName "{version_name}"', content)
    content = re.sub(r'versionCode\s+\d+',
                     f'versionCode {vc}', content)
    if not dry_run:
        gradle.write_text(content, encoding="utf-8")


# ============================================================================
# 第五步：构建 APK
# ============================================================================

def build_apk(target_dir, dry_run):
    env = os.environ.copy()
    env["FOLIA_DISABLE_PWA"] = "1"

    log("=" * 50)
    log("1/4 npm install")
    log("=" * 50)
    if not dry_run:
        run(["npm", "install", "--no-audit", "--no-fund"], cwd=target_dir, env=env)

    log("=" * 50)
    log("2/4 vite build")
    log("=" * 50)
    if not dry_run:
        run(["npx", "vite", "build"], cwd=target_dir, env=env)

    log("=" * 50)
    log("3/4 cap sync android")
    log("=" * 50)
    if not dry_run:
        run(["npx", "cap", "sync", "android"], cwd=target_dir, env=env)

    log("=" * 50)
    log("4/4 gradlew assembleDebug")
    log("=" * 50)
    android_dir = target_dir / "android"
    gradlew = android_dir / "gradlew"
    if not gradlew.exists():
        gradlew = android_dir / "gradlew.bat"

    if not dry_run:
        run([str(gradlew), "assembleDebug"], cwd=android_dir, env=env)

    apk = target_dir / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    if not dry_run:
        if not apk.exists():
            log(f"APK 未找到: {apk}", "ERROR")
            sys.exit(1)
        mb = apk.stat().st_size / 1024 / 1024
        log(f"APK 构建成功: {apk} ({mb:.1f} MB)")
    else:
        log(f"[dry-run] 将生成: {apk}")
    return apk


# ============================================================================
# 第六步：提交 & 推送 & 发布
# ============================================================================

def commit_and_push(target_dir, version, upstream_commit, dry_run):
    if dry_run:
        log("[dry-run] 跳过 commit & push")
        return

    # CI 环境：先配置 git 用户和 remote，再 commit
    if os.environ.get("GITHUB_WORKSPACE"):
        run(["git", "config", "user.name", "github-actions[bot]"], cwd=target_dir)
        run(["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"], cwd=target_dir)
        token = os.environ.get("GITHUB_TOKEN", "")
        run(["git", "remote", "set-url", "origin",
             f"https://x-access-token:{token}@github.com/{TARGET_OWNER}/{TARGET_REPO}.git"],
            cwd=target_dir)

    run(["git", "add", "-A"], cwd=target_dir)
    r = run(["git", "diff", "--cached", "--quiet"], cwd=target_dir, check=False)
    if r.returncode == 0:
        log("无变更，跳过 push")
        return

    msg = (f"chore(sync): 同步上游 folia-major@{upstream_commit[:8]} → v{version}\n\n"
           f"- 同步 chthollyphile/folia-major/{UPSTREAM_BRANCH} 最新代码\n"
           f"- 保留 Android 专属文件\n"
           f"- versionName={version}")
    run(["git", "commit", "-m", msg], cwd=target_dir)
    run(["git", "push", "origin", TARGET_BRANCH], cwd=target_dir)
    log("推送完成")


def create_release(target_dir, version, apk_path, token, dry_run):
    tag = f"v{version}"
    name = f"Folia Android v{version}"
    body = (f"自动同步上游 [chthollyphile/folia-major]"
            f"(https://github.com/chthollyphile/folia-major) 构建。\n\n"
            f"- 版本: v{version}\n"
            f"- 构建时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"下载下方 APK 安装。")

    log(f"创建 Release: {tag}")
    if dry_run:
        log(f"[dry-run] 将创建 {tag} 并上传 {apk_path.name}")
        return

    # 创建或获取 release
    try:
        release = gh_api("POST", f"/repos/{TARGET_OWNER}/{TARGET_REPO}/releases",
                         token, {
                             "tag_name": tag,
                             "target_commitish": TARGET_BRANCH,
                             "name": name,
                             "body": body,
                             "draft": False,
                             "prerelease": False,
                         })
    except SystemExit:
        log("Release 已存在，查询 ...")
        releases = gh_api("GET", f"/repos/{TARGET_OWNER}/{TARGET_REPO}/releases", token)
        release = next((r for r in releases if r.get("tag_name") == tag), None)
        if not release:
            raise

    log(f"Release: {release['html_url']}")

    # 上传 APK
    apk_name = f"folia-android-v{version}.apk"
    upload_url = release["upload_url"].replace("{?name,label}", "")
    final_url = f"{upload_url}?name={urllib.parse.quote(apk_name)}"

    data = apk_path.read_bytes()
    log(f"上传 {apk_name} ({len(data)/1024/1024:.1f} MB)")

    def do_upload(url, payload):
        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Authorization": f"token {token}",
                "Content-Type": "application/vnd.android.package-archive",
                "Content-Length": str(len(payload)),
                "User-Agent": "folia-android-sync",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    try:
        asset = do_upload(final_url, data)
    except urllib.error.HTTPError as e:
        if e.code in (422, 409):
            log("APK 已存在，删除后重传 ...")
            assets = gh_api("GET",
                f"/repos/{TARGET_OWNER}/{TARGET_REPO}/releases/{release['id']}/assets", token)
            for a in assets:
                if a["name"] == apk_name:
                    gh_api("DELETE",
                        f"/repos/{TARGET_OWNER}/{TARGET_REPO}/releases/assets/{a['id']}", token)
            asset = do_upload(final_url, data)
        else:
            raise

    log(f"APK 上传成功: {asset['browser_download_url']}")


# ============================================================================
# 主流程
# ============================================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        log("错误: 请设置 GITHUB_TOKEN", "ERROR")
        sys.exit(1)

    log("=" * 50)
    log("folia-android 同步 & 构建 & 发布")
    log("=" * 50)
    log(f"模式: {'dry-run' if args.dry_run else '正式'}")
    log(f"CI: {'是' if os.environ.get('GITHUB_WORKSPACE') else '否'}")

    # 1. 准备目录
    target_dir, upstream_dir = setup_workdir()

    # 读上游版本
    upstream_pkg = json.loads((upstream_dir / "package.json").read_text(encoding="utf-8"))
    upstream_version = upstream_pkg["version"]
    upstream_commit = run(["git", "rev-parse", "HEAD"],
                          cwd=upstream_dir, capture=True).stdout.strip()
    log(f"上游版本: v{upstream_version} ({upstream_commit[:8]})")

    if not args.build_only:
        # 2. 同步文件
        log("\n--- 同步上游文件 ---")
        sync_files_from_upstream(upstream_dir, target_dir, args.dry_run)

        # 3. 合并 package.json
        log("\n--- 合并 package.json ---")
        pkg = merge_package_json(upstream_dir, target_dir, args.dry_run)
        version = pkg["version"]

        # 4. 更新 Android 版本号
        update_android_version(target_dir, version, args.dry_run)

        # 5. 提交推送
        commit_and_push(target_dir, version, upstream_commit, args.dry_run)
    else:
        cur = json.loads((target_dir / "package.json").read_text(encoding="utf-8"))
        version = cur["version"]

    # 6. 构建 APK
    log("\n--- 构建 APK ---")
    apk = build_apk(target_dir, args.dry_run)

    # 7. 发布
    log("\n--- 发布 Release ---")
    create_release(target_dir, version, apk, token, args.dry_run)

    log("\n全部完成！")


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
