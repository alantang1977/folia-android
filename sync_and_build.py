#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
folia-android 上游同步 & APK 自动构建发布脚本
================================================

功能：
  1. 从上游 chthollyphile/folia-major 拉取最新 Web 前端代码
  2. 合并到你的 alantang1977/folia-android 仓库，保留 Android 专属文件
  3. 合并 package.json（注入 Capacitor 依赖）
  4. 自动构建可安装的 APK
  5. 创建 GitHub Release 并上传 APK

用法：
  export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx   # 有 repo 权限的 PAT
  python3 sync_and_build.py                      # 完整流程：同步+构建+发布
  python3 sync_and_build.py --sync-only          # 只同步代码，不构建
  python3 sync_and_build.py --build-only         # 只构建当前代码，不同步
  python3 sync_and_build.py --dry-run            # 只打印计划，不执行写操作

在 GitHub Actions 中运行时，GITHUB_TOKEN 由 runner 自动注入，无需手动设置。
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
# 配置区 —— 按需修改
# ============================================================================

UPSTREAM_REPO_OWNER = "chthollyphile"
UPSTREAM_REPO_NAME = "folia-major"
UPSTREAM_BRANCH = "main"

ANDROID_BASE_REPO_OWNER = "Timingyyds"   # 参考的 Android 移植版
ANDROID_BASE_REPO_NAME = "folia-android"

TARGET_REPO_OWNER = "alantang1977"      # 你的仓库
TARGET_REPO_NAME = "folia-android"
TARGET_BRANCH = "main"

# ---- 从上游同步的目录（Web 前端，不含 Android 专属内容）----
SYNC_DIRS = [
    "src", "shared", "public", "mods", "assets", "skills",
    "dev", "docs", "build", "packaging", "sync-server",
    "native", "test", "worker", "api", "api-ts", "img",
    "electron",
]

# ---- 从上游同步的顶层配置文件 ----
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

# ---- 绝对保留，绝不用上游覆盖的路径（Android 专属）----
PRESERVE_PATHS = [
    "android",                  # Capacitor Android 工程目录
    "capacitor.config.json",    # Capacitor 配置
    "README.md",                # Android 版专属 README
    "mu.txt", "qr_base64.txt", "qr_dataurl.txt",  # Android 版遗留文件
    ".github",                  # GitHub Actions 工作流
    ".gemini",                  # Gemini 配置
]

# ---- package.json 中需要注入的 Android 专属依赖 ----
ANDROID_DEPENDENCIES = {
    "@capacitor/android": "^8.5.3",
    "@capacitor/cli": "^8.5.3",
    "@capacitor/core": "^8.5.3",
    "@capacitor/filesystem": "^8.1.5",
    "@capacitor/splash-screen": "^8.0.2",
    "@capacitor/status-bar": "^8.0.4",
    "crypto-es": "^3.1.3",
    "qrcode": "^1.5.4",
}

ANDROID_DEV_DEPENDENCIES = {
    "@types/qrcode": "^1.5.6",
}

# ---- Android 应用信息 ----
ANDROID_APP_ID = "top.izuna.foliamajor"
ANDROID_APP_NAME = "Folia"

# ---- 构建输出路径 ----
APK_DEBUG_PATH = "android/app/build/outputs/apk/debug/app-debug.apk"
APK_RELEASE_PATH = "android/app/build/outputs/apk/release/app-release.apk"

# ============================================================================
# 工具函数
# ============================================================================

def log(msg: str, level: str = "INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)


def run(cmd: list[str], cwd: str | Path = ".", env: dict | None = None,
        check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    """执行 shell 命令，实时输出或捕获输出。"""
    log(f"$ {' '.join(str(c) for c in cmd)}  (cwd={cwd})")
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    if capture:
        result = subprocess.run(
            cmd, cwd=str(cwd), env=merged_env,
            capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            cmd, cwd=str(cwd), env=merged_env,
        )
    if check and result.returncode != 0:
        log(f"命令失败 (exit={result.returncode}): {' '.join(str(c) for c in cmd)}", "ERROR")
        if capture:
            log(f"stdout: {result.stdout[-2000:]}", "ERROR")
            log(f"stderr: {result.stderr[-2000:]}", "ERROR")
        sys.exit(1)
    return result


def github_api(method: str, path: str, token: str, body: dict | None = None) -> dict:
    """调用 GitHub REST API。"""
    url = f"https://api.github.com{path}"
    data = None
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "folia-android-sync-script",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        log(f"GitHub API {method} {path} 返回 {e.code}: {err_body}", "ERROR")
        raise


# ============================================================================
# 第一步：克隆/准备仓库
# ============================================================================

def prepare_repos(workdir: Path, token: str, dry_run: bool) -> tuple[Path, Path]:
    """
    克隆上游仓库和目标 Android 仓库到工作目录。
    返回 (upstream_dir, target_dir)。
    """
    upstream_dir = workdir / "upstream"
    target_dir = workdir / "target"

    upstream_url = f"https://github.com/{UPSTREAM_REPO_OWNER}/{UPSTREAM_REPO_NAME}.git"
    target_url = f"https://{token}@github.com/{TARGET_REPO_OWNER}/{TARGET_REPO_NAME}.git"

    # 浅克隆上游
    if not upstream_dir.exists():
        log(f"克隆上游仓库 {upstream_url} ...")
        run(["git", "clone", "--depth", "1", "--branch", UPSTREAM_BRANCH,
             upstream_url, str(upstream_dir)])
    else:
        log("更新上游仓库 ...")
        run(["git", "fetch", "origin", UPSTREAM_BRANCH], cwd=upstream_dir)
        run(["git", "reset", "--hard", f"origin/{UPSTREAM_BRANCH}"], cwd=upstream_dir)

    # 浅克隆目标仓库
    if not target_dir.exists():
        log(f"克隆目标仓库 {TARGET_REPO_OWNER}/{TARGET_REPO_NAME} ...")
        run(["git", "clone", "--depth", "50", "--branch", TARGET_BRANCH,
             target_url, str(target_dir)])
    else:
        log("更新目标仓库 ...")
        run(["git", "fetch", "origin", TARGET_BRANCH], cwd=target_dir)
        run(["git", "reset", "--hard", f"origin/{TARGET_BRANCH}"], cwd=target_dir)

    return upstream_dir, target_dir


# ============================================================================
# 第二步：同步文件
# ============================================================================

def is_preserved(rel_path: str) -> bool:
    """判断某个相对路径是否属于 Android 专属、需要保留。"""
    rel = rel_path.replace("\\", "/").lstrip("./")
    for p in PRESERVE_PATHS:
        p = p.replace("\\", "/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def sync_directories(upstream_dir: Path, target_dir: Path, dry_run: bool):
    """把上游目录复制到目标仓库，跳过保留路径。"""
    for dirname in SYNC_DIRS:
        src = upstream_dir / dirname
        dst = target_dir / dirname
        if not src.exists():
            log(f"上游目录不存在，跳过: {dirname}")
            continue
        if is_preserved(dirname):
            log(f"保留路径，跳过同步: {dirname}")
            continue

        # 先删除目标中的旧目录（确保干净同步）
        if dst.exists():
            log(f"删除旧目录: {dirname}/")
            if not dry_run:
                shutil.rmtree(dst)

        # 复制新目录
        log(f"同步目录: {dirname}/")
        if not dry_run:
            shutil.copytree(src, dst, symlinks=True)


def sync_files(upstream_dir: Path, target_dir: Path, dry_run: bool):
    """把上游顶层配置文件复制到目标仓库。"""
    for filename in SYNC_FILES:
        src = upstream_dir / filename
        dst = target_dir / filename
        if not src.exists():
            log(f"上游文件不存在，跳过: {filename}")
            continue
        if is_preserved(filename):
            log(f"保留路径，跳过同步: {filename}")
            continue
        log(f"同步文件: {filename}")
        if not dry_run:
            shutil.copy2(src, dst)


# ============================================================================
# 第三步：合并 package.json
# ============================================================================

def merge_package_json(upstream_dir: Path, target_dir: Path, dry_run: bool) -> dict:
    """
    以上游 package.json 为基础，注入 Android 专属依赖，
    保留 Android 的 name 字段和构建所需配置。
    """
    upstream_pkg_path = upstream_dir / "package.json"
    with open(upstream_pkg_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    # 名称改为 Android 版
    pkg["name"] = "folia-android"

    # 注入 Android 专属 dependencies
    deps = pkg.get("dependencies", {})
    for k, v in ANDROID_DEPENDENCIES.items():
        deps[k] = v
    pkg["dependencies"] = deps

    # 注入 Android 专属 devDependencies
    dev_deps = pkg.get("devDependencies", {})
    for k, v in ANDROID_DEV_DEPENDENCIES.items():
        dev_deps[k] = v
    pkg["devDependencies"] = dev_deps

    # 确保 build 脚本兼容 Android（不依赖 electron 打包）
    # 保留上游 scripts，构建时我们直接用 npx vite build
    # 不需要修改 scripts

    # 写入目标仓库
    dst = target_dir / "package.json"
    log(f"合并 package.json → name={pkg['name']}, version={pkg['version']}, "
        f"deps={len(deps)}, devDeps={len(dev_deps)}")
    if not dry_run:
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(pkg, f, indent=2, ensure_ascii=False)
            f.write("\n")

    return pkg


# ============================================================================
# 第四步：更新 Android 版本号
# ============================================================================

def version_to_code(version: str) -> int:
    """
    把语义化版本号转为 versionCode。
    例: 0.7.25 → 725；2.3.0 → 230
    最多取三段，每段两位数字，拼成整数。
    """
    parts = re.findall(r"\d+", version)
    nums = [int(p) for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    code = nums[0] * 10000 + nums[1] * 100 + nums[2]
    return max(code, 1)


def update_android_version(target_dir: Path, version_name: str, dry_run: bool):
    """修改 android/app/build.gradle 中的 versionName 和 versionCode。"""
    gradle_path = target_dir / "android" / "app" / "build.gradle"
    if not gradle_path.exists():
        log(f"未找到 {gradle_path}，跳过版本号更新", "WARN")
        return

    version_code = version_to_code(version_name)
    log(f"设置 Android 版本: versionName={version_name}, versionCode={version_code}")

    content = gradle_path.read_text(encoding="utf-8")
    # 替换 versionName
    content = re.sub(
        r'versionName\s+["\'][^"\']*["\']',
        f'versionName "{version_name}"',
        content,
    )
    # 替换 versionCode
    content = re.sub(
        r'versionCode\s+\d+',
        f'versionCode {version_code}',
        content,
    )
    if not dry_run:
        gradle_path.write_text(content, encoding="utf-8")


# ============================================================================
# 第五步：构建 APK
# ============================================================================

def build_apk(target_dir: Path, dry_run: bool) -> Path:
    """
    在目标仓库目录中执行完整构建：
      1. npm install
      2. vite build（禁用 PWA）
      3. cap sync android
      4. gradlew assembleDebug / assembleRelease
    返回生成的 APK 路径。
    """
    env = os.environ.copy()
    env["FOLIA_DISABLE_PWA"] = "1"

    # 1. 安装依赖
    log("=" * 60)
    log("步骤 1/4: npm install")
    log("=" * 60)
    if not dry_run:
        run(["npm", "install", "--no-audit", "--no-fund"], cwd=target_dir, env=env)

    # 2. 构建 Web 产物
    log("=" * 60)
    log("步骤 2/4: vite build (FOLIA_DISABLE_PWA=1)")
    log("=" * 60)
    if not dry_run:
        run(["npx", "vite", "build"], cwd=target_dir, env=env)

    # 3. 同步到 Android 工程
    log("=" * 60)
    log("步骤 3/4: npx cap sync android")
    log("=" * 60)
    if not dry_run:
        run(["npx", "cap", "sync", "android"], cwd=target_dir, env=env)

    # 4. 构建 APK
    log("=" * 60)
    log("步骤 4/4: gradlew assembleDebug")
    log("=" * 60)
    android_dir = target_dir / "android"
    gradlew = android_dir / "gradlew"
    if not gradlew.exists():
        # Windows 兼容
        gradlew = android_dir / "gradlew.bat"

    use_release = bool(
        os.environ.get("RELEASE_KEYSTORE_PATH")
        and os.environ.get("RELEASE_KEYSTORE_PASSWORD")
        and os.environ.get("RELEASE_KEY_ALIAS")
        and os.environ.get("RELEASE_KEY_PASSWORD")
    )

    task = "assembleRelease" if use_release else "assembleDebug"
    log(f"Gradle 任务: {task}")

    if not dry_run:
        run([str(gradlew), task], cwd=android_dir, env=env)

    apk_rel = APK_RELEASE_PATH if use_release else APK_DEBUG_PATH
    apk_path = target_dir / apk_rel
    if not dry_run:
        if not apk_path.exists():
            log(f"APK 未找到: {apk_path}", "ERROR")
            sys.exit(1)
        size_mb = apk_path.stat().st_size / 1024 / 1024
        log(f"APK 构建成功: {apk_path} ({size_mb:.1f} MB)")
    else:
        log(f"[dry-run] 将生成 APK: {apk_path}")

    return apk_path


# ============================================================================
# 第六步：提交、推送、发布 Release
# ============================================================================

def commit_and_push(target_dir: Path, version_name: str, upstream_commit: str, dry_run: bool):
    """提交同步变更并推送到目标仓库。"""
    log("提交同步变更 ...")
    if dry_run:
        log("[dry-run] 跳过 git commit & push")
        return

    run(["git", "add", "-A"], cwd=target_dir)

    # 检查是否有变更
    result = run(["git", "diff", "--cached", "--quiet"], cwd=target_dir, check=False)
    if result.returncode == 0:
        log("没有需要提交的变更，跳过 push")
        return

    commit_msg = (
        f"chore(sync): 同步上游 folia-major@{upstream_commit[:8]} → v{version_name}\n\n"
        f"- 从 chthollyphile/folia-major/{UPSTREAM_BRANCH} 同步最新 Web 前端代码\n"
        f"- 保留 Android 专属文件（android/、capacitor.config.json、Java 插件）\n"
        f"- 合并 package.json，注入 Capacitor 依赖\n"
        f"- Android versionName={version_name}\n"
    )
    run(["git", "commit", "-m", commit_msg], cwd=target_dir)
    run(["git", "push", "origin", TARGET_BRANCH], cwd=target_dir)
    log("推送完成")


def create_github_release(target_dir: Path, version_name: str, apk_path: Path,
                          token: str, dry_run: bool):
    """创建 GitHub Release 并上传 APK。"""
    tag = f"v{version_name}"
    release_name = f"Folia Android v{version_name}"
    body = (
        f"自动同步上游 [chthollyphile/folia-major]"
        f"(https://github.com/{UPSTREAM_REPO_OWNER}/{UPSTREAM_REPO_NAME}) 最新代码构建。\n\n"
        f"- 上游版本: v{version_name}\n"
        f"- 构建时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"- 包名: {ANDROID_APP_ID}\n\n"
        f"下载下方 APK 安装即可。\n"
    )

    log(f"创建 GitHub Release: {tag}")
    if dry_run:
        log(f"[dry-run] 将创建 release {tag} 并上传 {apk_path.name}")
        return

    # 创建 release
    try:
        release = github_api("POST", f"/repos/{TARGET_REPO_OWNER}/{TARGET_REPO_NAME}/releases",
                             token, {
                                 "tag_name": tag,
                                 "target_commitish": TARGET_BRANCH,
                                 "name": release_name,
                                 "body": body,
                                 "draft": False,
                                 "prerelease": False,
                             })
    except SystemExit:
        # tag 已存在，尝试更新已有 release
        log("Release 可能已存在，尝试查询 ...")
        releases = github_api("GET",
                               f"/repos/{TARGET_REPO_OWNER}/{TARGET_REPO_NAME}/releases",
                               token)
        release = next((r for r in releases if r.get("tag_name") == tag), None)
        if not release:
            raise

    upload_url = release["upload_url"].replace("{?name,label}", "")
    log(f"Release 创建成功: {release['html_url']}")

    # 上传 APK
    apk_name = f"folia-android-v{version_name}.apk"
    log(f"上传 APK: {apk_name} ({apk_path.stat().st_size / 1024 / 1024:.1f} MB)")

    upload_url_final = f"{upload_url}?name={urllib.parse.quote(apk_name)}"
    with open(apk_path, "rb") as f:
        data = f.read()

    req = urllib.request.Request(
        upload_url_final,
        data=data,
        headers={
            "Authorization": f"token {token}",
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Length": str(len(data)),
            "User-Agent": "folia-android-sync-script",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            asset = json.loads(resp.read().decode("utf-8"))
        log(f"APK 上传成功: {asset['browser_download_url']}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        # 如果 asset 已存在，先删除再传
        if e.code in (422, 409):
            log("APK asset 已存在，删除后重传 ...")
            assets = github_api("GET",
                                f"/repos/{TARGET_REPO_OWNER}/{TARGET_REPO_NAME}/releases/{release['id']}/assets",
                                token)
            for a in assets:
                if a["name"] == apk_name:
                    github_api("DELETE",
                               f"/repos/{TARGET_REPO_OWNER}/{TARGET_REPO_NAME}/releases/assets/{a['id']}",
                               token)
            # 重传
            req2 = urllib.request.Request(
                upload_url_final, data=data,
                headers={
                    "Authorization": f"token {token}",
                    "Content-Type": "application/vnd.android.package-archive",
                    "Content-Length": str(len(data)),
                    "User-Agent": "folia-android-sync-script",
                },
                method="POST",
            )
            with urllib.request.urlopen(req2) as resp:
                asset = json.loads(resp.read().decode("utf-8"))
            log(f"APK 重新上传成功: {asset['browser_download_url']}")
        else:
            log(f"上传失败 {e.code}: {err_body}", "ERROR")
            raise


# ============================================================================
# 主流程
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="folia-android 上游同步 & APK 自动构建发布脚本"
    )
    parser.add_argument("--sync-only", action="store_true",
                        help="只同步代码到仓库，不构建 APK")
    parser.add_argument("--build-only", action="store_true",
                        help="只构建当前代码，不同步上游")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划，不执行写操作")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not args.dry_run:
        log("错误: 请设置环境变量 GITHUB_TOKEN (有 repo 权限的 GitHub PAT)", "ERROR")
        log("  export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx")
        sys.exit(1)

    log("=" * 60)
    log("folia-android 上游同步 & 构建发布")
    log("=" * 60)
    log(f"上游: {UPSTREAM_REPO_OWNER}/{UPSTREAM_REPO_NAME}@{UPSTREAM_BRANCH}")
    log(f"目标: {TARGET_REPO_OWNER}/{TARGET_REPO_NAME}@{TARGET_BRANCH}")
    log(f"模式: {'dry-run' if args.dry_run else '正式执行'}")
    log("")

    with tempfile.TemporaryDirectory(prefix="folia_sync_") as tmp:
        workdir = Path(tmp)
        log(f"工作目录: {workdir}")

        # ---- 准备仓库 ----
        upstream_dir, target_dir = prepare_repos(workdir, token, args.dry_run)

        # 获取上游最新 commit hash 和版本号
        upstream_pkg = json.loads((upstream_dir / "package.json").read_text(encoding="utf-8"))
        upstream_version = upstream_pkg["version"]
        upstream_commit = run(
            ["git", "rev-parse", "HEAD"], cwd=upstream_dir, capture=True
        ).stdout.strip()
        log(f"上游最新版本: v{upstream_version} (commit {upstream_commit[:8]})")

        if not args.build_only:
            # ---- 同步文件 ----
            log("")
            log("-" * 60)
            log("开始同步上游文件 ...")
            log("-" * 60)
            sync_directories(upstream_dir, target_dir, args.dry_run)
            sync_files(upstream_dir, target_dir, args.dry_run)

            # ---- 合并 package.json ----
            log("")
            log("-" * 60)
            log("合并 package.json ...")
            log("-" * 60)
            merged_pkg = merge_package_json(upstream_dir, target_dir, args.dry_run)
            android_version = merged_pkg["version"]

            # ---- 更新 Android 版本号 ----
            update_android_version(target_dir, android_version, args.dry_run)

            # ---- 提交 & 推送 ----
            commit_and_push(target_dir, android_version, upstream_commit, args.dry_run)
        else:
            # build-only 模式：读取当前 package.json 版本
            current_pkg = json.loads((target_dir / "package.json").read_text(encoding="utf-8"))
            android_version = current_pkg["version"]

        if args.sync_only:
            log("")
            log("--sync-only 模式，跳过构建和发布")
            log("完成。")
            return

        # ---- 构建 APK ----
        log("")
        log("=" * 60)
        log("开始构建 APK ...")
        log("=" * 60)
        apk_path = build_apk(target_dir, args.dry_run)

        # ---- 发布 Release ----
        log("")
        log("=" * 60)
        log("发布 GitHub Release ...")
        log("=" * 60)
        create_github_release(target_dir, android_version, apk_path, token, args.dry_run)

    log("")
    log("=" * 60)
    log("全部完成！")
    log("=" * 60)


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
