#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
folia-android 同步 & APK 构建发布脚本（Timingyyds 主基准版）
================================================

优先级：
  1. Timingyyds/folia-android 为底基 —— 必须保证安卓设备上所有功能正常
  2. chthollyphile/folia-major 为辅 —— 只增量叠加新文件/新功能，不覆盖安卓代码

策略：
  - 以 Timingyyds 完整代码为基准
  - 上游独有的新文件 → 新增到仓库
  - Timingyyds 已有且与上游不同的文件 → 保留 Timingyyds 版本（不破坏安卓功能）
  - package.json 合并：保留安卓依赖 + 加入上游新依赖
  - 构建 APK → 发 GitHub Release

用法：
  export GITHUB_TOKEN=ghp_xxxxx
  python3 sync_and_build.py              # 完整流程
  python3 sync_and_build.py --build-only # 只构建
  python3 sync_and_build.py --dry-run    # 预览
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
# 配置
# ============================================================================

# Android 主基准仓库（必须保留的功能底基）
# 锁定 v2.3.0 release tag，不用 main 分支
ANDROID_BASE_REPO = "https://github.com/Timingyyds/folia-android.git"
ANDROID_BASE_BRANCH = "v2.3.0"

# 上游辅助仓库（只取新功能）
# 自动检测最新 tag，不用固定 main
UPSTREAM_REPO = "https://github.com/chthollyphile/folia-major.git"
UPSTREAM_BRANCH = "main"  # 作为 fallback，实际会自动检测最新 tag

# 你的仓库
TARGET_OWNER = "alantang1977"
TARGET_REPO = "folia-android"
TARGET_BRANCH = "main"

# 上游目录中，Timingyyds 没有的、可以直接全量新增的目录
ADDITIVE_DIRS = [
    "api", "api-ts", "worker", "img", "deploy",
]

# 上游和 Timingyyds 共有的目录 → 只增量新增上游独有文件，不覆盖已有文件
SHARED_DIRS = [
    "src", "shared", "public", "mods", "assets", "skills",
    "dev", "docs", "build", "packaging", "sync-server",
    "native", "test", "electron",
]

# 安卓关键目录：每次都从 Timingyyds 完整覆盖（不做增量）
# 这些是安卓桥接核心代码，必须完整无误
FORCE_RESTORE_DIRS = [
    "src/audioNative",
]

# 安卓特定文件/目录：必须从 Timingyyds 保留（不被上游覆盖）
# 这些文件集成了安卓桥接，用上游版本会丢失安卓功能
ANDROID_PATHS = [
    "src/audioNative",
    "src/svc",
    "src/App.tsx",
    "src/bootstrap.tsx",
    "src/index.tsx",
    "src/services/onlineMusic",
    "src/services/automix/stems.ts",
]

# 顶层文件：仅当 Timingyyds 没有时才新增
ADDITIVE_TOP_FILES = [
    "vercel.json", "wrangler.jsonc",
    "AGENTS.md", "CLAUDE.md", "README.en.md",
    "CONTRIBUTING.en.md", "CONTRIBUTING.md", "CONTRIBUTORS.md",
]

# 绝对保留的路径（不被任何操作覆盖）
PRESERVE_PATHS = [
    "android",
    "capacitor.config.json",
    "README.md",
    "mu.txt", "qr_base64.txt", "qr_dataurl.txt",
    ".github",
    ".gemini",
    "sync_and_build.py",
    "realeco-release",
]

# 安卓必须的依赖（永远保留）
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
# 工具
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
# 第一步：克隆两个参考仓库
# ============================================================================

def get_latest_upstream_tag():
    """自动检测上游最新正式版本 tag（vX.Y.Z 格式，排除 canary/beta）"""
    import subprocess as sp
    log("检测上游最新版本 tag ...")
    r = sp.run(
        ["git", "ls-remote", "--tags", UPSTREAM_REPO],
        capture_output=True, text=True,
    )
    tags = []
    for line in r.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        tag = parts[1].replace("refs/tags/", "").replace("^{}", "")
        # 只要正式版本 vX.Y.Z
        if re.match(r"^v\d+\.\d+\.\d+$", tag):
            tags.append(tag)
    if not tags:
        log("未检测到正式 tag，使用 main 分支", "WARN")
        return "main"
    # 按版本号排序取最新
    def ver_key(t):
        return [int(x) for x in re.findall(r"\d+", t)]
    latest = sorted(tags, key=ver_key)[-1]
    log(f"上游最新版本: {latest}")
    return latest


def setup():
    ci_ws = os.environ.get("GITHUB_WORKSPACE", "")
    if ci_ws:
        target_dir = Path(ci_ws)
        log(f"CI 工作区: {target_dir}")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="folia_"))
        target_dir = tmp / "target"
        token = os.environ.get("GITHUB_TOKEN", "")
        url = f"https://{token}@github.com/{TARGET_OWNER}/{TARGET_REPO}.git"
        run(["git", "clone", "--depth", "50", "--branch", TARGET_BRANCH,
             url, str(target_dir)])

    # 克隆 Timingyyds v2.3.0（安卓主基准）
    ab_dir = Path(tempfile.mkdtemp(prefix="folia_base_"))
    log(f"克隆 Android 主基准: {ANDROID_BASE_REPO} @ {ANDROID_BASE_BRANCH}")
    run(["git", "clone", "--depth", "1", "--branch", ANDROID_BASE_BRANCH,
         ANDROID_BASE_REPO, str(ab_dir)])

    # 克隆上游最新版本（自动检测 tag）
    up_tag = get_latest_upstream_tag()
    up_dir = Path(tempfile.mkdtemp(prefix="folia_upstream_"))
    log(f"克隆上游: {UPSTREAM_REPO} @ {up_tag}")
    run(["git", "clone", "--depth", "1", "--branch", up_tag,
         UPSTREAM_REPO, str(up_dir)])

    return target_dir, ab_dir, up_dir, up_tag


# ============================================================================
# 第二步：以 Timingyyds 为基准 + 叠加上游新文件
# ============================================================================

def is_preserved(rel):
    rel = rel.replace("\\", "/").lstrip("./")
    for p in PRESERVE_PATHS:
        p = p.replace("\\", "/")
        if rel == p or rel.startswith(p + "/"):
            return True
    return False


def sync_additive(ab_dir, up_dir, target_dir, dry_run):
    """
    核心逻辑：
    1. 先确保 target_dir 里有 Timingyyds 的完整代码（安卓功能底基）
    2. 再向上叠加上游独有的新文件（新功能）
    3. Timingyyds 已有的文件永不覆盖
    """
def is_android_path(rel_str):
    """判断是否是安卓特定路径（必须从 Timingyyds 保留）"""
    rel_str = rel_str.replace("\\", "/").lstrip("./")
    for p in ANDROID_PATHS:
        p = p.replace("\\", "/")
        if rel_str == p or rel_str.startswith(p + "/"):
            return True
    return False


def sync_additive(ab_dir, up_dir, target_dir, dry_run):
    """
    新策略：上游最新 Web 代码为底基 + Timingyyds 安卓桥接覆盖
    - 上游 src/ 作为底基（最新 UI，无版本不匹配）
    - Timingyyds 的安卓特定文件覆盖上去（audioNative、App.tsx 等）
    - 上游独有目录全量新增
    """
    # ---- 第 1 步：上游 src/ 作为底基（覆盖到 target）----
    log("\n--- 步骤1: 上游最新 src/ 作为底基 ---")
    up_src = up_dir / "src"
    dst_src = target_dir / "src"
    if up_src.exists():
        # 先删除 target 的 src/（旧的 Timingyyds 版本）
        if dst_src.exists():
            log("  删除旧 src/，替换为上游最新版")
            if not dry_run:
                shutil.rmtree(dst_src)
        log("  复制上游 src/ 底基")
        if not dry_run:
            shutil.copytree(up_src, dst_src, symlinks=True)

    # ---- 第 2 步：Timingyyds 安卓特定文件覆盖上去 ----
    log("\n--- 步骤2: 覆盖 Timingyyds 安卓桥接文件 ---")
    android_files_count = 0
    for root, dirs, files in os.walk(ab_dir / "src"):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            ab_file = Path(root) / f
            rel = ab_file.relative_to(ab_dir / "src")
            rel_str = f"src/{rel}"
            if not is_android_path(rel_str):
                continue
            dst_file = target_dir / rel_str
            if not dry_run:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ab_file, dst_file)
            android_files_count += 1
    log(f"安卓桥接文件覆盖: {android_files_count} 个")

    # ---- 第 2.5 步：修补缺失的 reference/router stub ----
    ref_dir = target_dir / "src" / "audioNative" / "reference"
    ref_router = ref_dir / "router.ts"
    if not ref_router.exists():
        log("  生成 stub: src/audioNative/reference/router.ts")
        if not dry_run:
            ref_dir.mkdir(parents=True, exist_ok=True)
            ref_router.write_text(
                "// 自动生成的 stub —— reference/ 目录为开发参考用，不提交 git\n"
                "// 构建时提供空实现，避免 vite build 报错\n\n"
                "interface RequestOptions {\n"
                "  path: string;\n"
                "  method?: string;\n"
                "  query?: Record<string, string>;\n"
                "  body?: unknown;\n"
                "}\n\n"
                "export async function handleApiRequest(_options: RequestOptions): Promise<unknown> {\n"
                "  return { code: -1, message: 'reference api not available in build' };\n"
                "}\n",
                encoding="utf-8",
            )

    # ---- 第 3 步：上游独有目录全量新增 ----
    log("\n--- 步骤3: 新增上游独有目录 ---")
    for dirname in ADDITIVE_DIRS:
        up_d = up_dir / dirname
        if not up_d.exists():
            continue
        ab_d = ab_dir / dirname
        if ab_d.exists():
            log(f"  跳过(已有): {dirname}/")
            continue
        dst = target_dir / dirname
        if dst.exists():
            log(f"  跳过(目标已有): {dirname}/")
            continue
        log(f"  新增上游目录: {dirname}/")
        if not dry_run:
            shutil.copytree(up_d, dst, symlinks=True)

    # ---- 第 4 步：其他共享目录用上游最新版 ----
    log("\n--- 步骤4: 其他共享目录用上游最新版 ---")
    for dirname in SHARED_DIRS:
        if dirname == "src":
            continue  # src 已在步骤1处理
        up_d = up_dir / dirname
        if not up_d.exists():
            continue
        if is_preserved(dirname):
            continue
        dst_d = target_dir / dirname
        if dst_d.exists():
            if not dry_run:
                shutil.rmtree(dst_d)
        log(f"  替换为上游版: {dirname}/")
        if not dry_run:
            shutil.copytree(up_d, dst_d, symlinks=True)

    # ---- 第 5 步：顶层文件用上游最新版（保留 README）----
    log("\n--- 步骤5: 顶层文件同步 ---")
    for fname in ADDITIVE_TOP_FILES:
        up_f = up_dir / fname
        if not up_f.exists():
            continue
        dst = target_dir / fname
        log(f"  同步: {fname}")
        if not dry_run:
            shutil.copy2(up_f, dst)


# ============================================================================
# 第三步：合并 package.json
# ============================================================================

def merge_package_json(ab_dir, up_dir, target_dir, dry_run):
    """
    以 Timingyyds 的 package.json 为基准（保留安卓依赖），
    加入上游新增的依赖。
    """
    with open(ab_dir / "package.json", encoding="utf-8") as f:
        pkg = json.load(f)

    deps = pkg.get("dependencies", {})
    deps.update(ANDROID_DEPS)
    pkg["dependencies"] = deps

    dev = pkg.get("devDependencies", {})
    dev.update(ANDROID_DEV_DEPS)
    pkg["devDependencies"] = dev

    # 从上游加入 Timingyyds 没有的新依赖
    with open(up_dir / "package.json", encoding="utf-8") as f:
        up_pkg = json.load(f)

    up_deps = up_pkg.get("dependencies", {})
    added_deps = 0
    for k, v in up_deps.items():
        if k not in deps:
            deps[k] = v
            added_deps += 1
            log(f"  新增上游依赖: {k}@{v}")

    up_dev = up_pkg.get("devDependencies", {})
    added_dev = 0
    for k, v in up_dev.items():
        if k not in dev:
            dev[k] = v
            added_dev += 1
            log(f"  新增上游 devDep: {k}@{v}")

    pkg["dependencies"] = deps
    pkg["devDependencies"] = dev

    up_version = up_pkg.get("version", "unknown")
    log(f"package.json: deps={len(deps)}(+{added_deps}), devDeps={len(dev)}(+{added_dev})")
    log(f"上游参考版本: {up_version}")

    if not dry_run:
        with open(target_dir / "package.json", "w", encoding="utf-8") as f:
            json.dump(pkg, f, indent=2, ensure_ascii=False)
            f.write("\n")

    return pkg, up_version


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
        log(f"未找到 {gradle}", "WARN")
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
        gradlew.chmod(0o755)
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
# 第六步：提交 & 发布
# ============================================================================

def commit_and_push(target_dir, version, up_commit, dry_run):
    if dry_run:
        log("[dry-run] 跳过 commit & push")
        return

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

    msg = (f"chore: 同步上游新功能 + v{version}\n\n"
           f"- 以 Timingyyds/folia-android 安卓底基为准\n"
           f"- 叠加 chthollyphile/folia-major 新文件\n"
           f"- 上游 commit: {up_commit[:8]}")
    run(["git", "commit", "-m", msg], cwd=target_dir)
    run(["git", "push", "origin", TARGET_BRANCH], cwd=target_dir)
    log("推送完成")


def create_release(target_dir, version, apk_path, token, dry_run, up_tag=""):
    tag = f"v{version}"
    name = f"Folia Android v{version}"
    up_line = f"- 上游版本: {up_tag}\n" if up_tag else ""
    body = (f"以 [Timingyyds/folia-android v2.3.0]"
            f"(https://github.com/Timingyyds/folia-android/tree/v2.3.0) 为安卓底基，\n"
            f"叠加 [chthollyphile/folia-major]"
            f"(https://github.com/chthollyphile/folia-major) 最新功能构建。\n\n"
            f"- 安卓底基: v2.3.0\n"
            f"{up_line}"
            f"- 版本: v{version}\n"
            f"- 构建时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"下载下方 APK 安装。")

    log(f"创建 Release: {tag}")
    if dry_run:
        log(f"[dry-run] 将创建 {tag}")
        return

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
    log("folia-android 同步 & 构建")
    log("底基: Timingyyds/folia-android (安卓功能优先)")
    log("辅助: chthollyphile/folia-major (新增功能)")
    log("=" * 50)
    log(f"模式: {'dry-run' if args.dry_run else '正式'}")

    target_dir, ab_dir, up_dir, up_tag = setup()

    ab_pkg = json.loads((ab_dir / "package.json").read_text(encoding="utf-8"))
    up_pkg = json.loads((up_dir / "package.json").read_text(encoding="utf-8"))
    up_commit = run(["git", "rev-parse", "HEAD"],
                    cwd=up_dir, capture=True).stdout.strip()
    log(f"Timingyyds 底基: v{ab_pkg.get('version', '?')} ({ANDROID_BASE_BRANCH})")
    log(f"上游版本: {up_tag} / package.json v{up_pkg.get('version', '?')} ({up_commit[:8]})")

    # ---- 检查上游是否有新提交（无更新则跳过构建）----
    state_file = target_dir / ".upstream-sync"
    prev_up_commit = ""
    if state_file.exists():
        prev_up_commit = state_file.read_text(encoding="utf-8").strip()

    manual = os.environ.get("GITHUB_EVENT_NAME", "") == "workflow_dispatch"
    if prev_up_commit == up_commit and not manual and not args.build_only:
        log(f"\n上游无新提交（{up_commit[:8]}），跳过构建。")
        log("如需强制构建，请手动触发 workflow。")
        return

    if not args.build_only:
        log("\n--- 同步代码 ---")
        sync_additive(ab_dir, up_dir, target_dir, args.dry_run)

        log("\n--- 合并 package.json ---")
        pkg, up_ver = merge_package_json(ab_dir, up_dir, target_dir, args.dry_run)

        version = pkg.get("version", up_ver)
        update_android_version(target_dir, version, args.dry_run)

        commit_and_push(target_dir, version, up_commit, args.dry_run)

        # 记录当前上游 commit
        if not args.dry_run:
            state_file.write_text(up_commit, encoding="utf-8")
    else:
        cur = json.loads((target_dir / "package.json").read_text(encoding="utf-8"))
        version = cur.get("version", "0.0.0")

    log("\n--- 构建 APK ---")
    apk = build_apk(target_dir, args.dry_run)

    log("\n--- 发布 ---")
    create_release(target_dir, version, apk, token, args.dry_run, up_tag)

    log("\n全部完成！")


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
