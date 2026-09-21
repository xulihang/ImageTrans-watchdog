#!/usr/bin/env python3
"""
ImageTrans 客户端看门狗

定时检查远程服务端 /list 接口，确认以 default 开头的实例数量是否为预期的 3 个。
如果不足，则杀掉所有 ImageTrans 进程并重新启动 3 个实例。

在 macOS 上运行。

用法：
    python3 imagetrans_watchdog.py                # 常驻运行
    python3 imagetrans_watchdog.py --once         # 只检查一次（调试用）
    python3 imagetrans_watchdog.py --dry-run      # 只报告，不执行 kill/start
"""

import argparse
import json
import logging
import os
import posixpath
import signal
import ssl
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------- 配置

# 服务端 /list 接口
SERVER_URL = "https://service.basiccat.org:51043/list"

# 期望的实例数，以及 displayName 前缀
EXPECTED_COUNT = 3
NAME_PREFIX = "default"

# 三个 app bundle 的路径。
# 每个 bundle 内含各自的 jdk-23 和 ImageTrans.jar：
#   <app>/Contents/Resources/ImageTrans/jdk-23/Contents/Home/bin/java
#   <app>/Contents/Resources/ImageTrans/ImageTrans.jar
APP_PATHS = [
    "/Users/xulihang/imagetrans-server/ImageTrans.app",
    "/Users/xulihang/imagetrans-server/ImageTrans_副本.app",
    "/Users/xulihang/imagetrans-server/ImageTrans_副本2.app",
]

# bundle 内部 ImageTrans 的安装目录（相对于 .app）
RESOURCES_SUBPATH = posixpath.join("Contents", "Resources", "ImageTrans")

# 三个实例各自的项目目录。
# 对应 Server.bas:845 的 Main.currentProject.path，即传给 jar 的第一个位置参数。
#
# 注意：每个实例必须用不同的项目目录，否则会争抢同一个 captured.db 和项目文件。
PROJECT_PATHS = [
    "/Users/xulihang/imagetrans_projects/server-new/1/Untitled.itp",
    "/Users/xulihang/imagetrans_projects/server-new/2/Untitled.itp",
    "/Users/xulihang/imagetrans_projects/server-new/3/Untitled.itp",
]

# 检查间隔（秒）
CHECK_INTERVAL = 60

# 连续失败多少次才触发重启。设为 1 表示立即触发。
# 调大可以避免服务端短暂抖动导致的误重启。
FAILURE_THRESHOLD = 2

# 网络检查连续失败多少次才认为是真的不可用（而不是网络抖动）。
# 网络失败时不做任何重启动作，只是继续等。
NETWORK_FAILURE_THRESHOLD = 5

# 容器/路径类错误连续出现多少次就判定为配置错误，停止重启并告警。
# 配置填错时重启多少次都没用，只会空转。
CONFIG_ERROR_THRESHOLD = 2

# 杀掉进程后等待多久再启动（等端口和文件锁释放）
KILL_GRACE_SECONDS = 8

# 启动实例之间的间隔
START_INTERVAL_SECONDS = 5

# 启动后等待多久，再验证 /list 里的实例数
STARTUP_VERIFY_DELAY = 45

# 重启后的冷静期（秒），避免重启循环
RESTART_COOLDOWN = 300

# 用于匹配并杀掉进程的命令行特征
PROCESS_PATTERN = "ImageTrans.jar"

# 是否跳过 TLS 证书校验。
# service.basiccat.org:51043 用的是自签名证书时，需要设为 True。
VERIFY_TLS = False

# 日志文件；设为 None 则只输出到 stdout
LOG_FILE = "/Users/xulihang/Library/Logs/imagetrans_watchdog.log"

# ---------------------------------------------------------------- 日志


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    if LOG_FILE:
        os.makedirs(posixpath.dirname(LOG_FILE), exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


log = logging.getLogger("watchdog")

# ---------------------------------------------------------------- 检查


def fetch_instances():
    """请求 /list，返回实例列表；失败时抛异常。"""
    ctx = ssl.create_default_context()
    if not VERIFY_TLS:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(SERVER_URL, headers={"User-Agent": "imagetrans-watchdog"})
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        body = resp.read().decode("utf-8", errors="replace")

    data = json.loads(body)
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected response type: {type(data).__name__}")
    return data


def target_instances(instances):
    """筛出 displayName 以 default 开头的实例。"""
    result = []
    for item in instances:
        if not isinstance(item, dict):
            continue
        name = item.get("displayName") or ""
        if name.startswith(NAME_PREFIX):
            result.append(item)
    return result


def check():
    """返回 (是否健康, 说明)。异常时抛出。"""
    instances = fetch_instances()
    targets = target_instances(instances)

    if len(targets) == EXPECTED_COUNT:
        return True, f"ok: {len(targets)} 个实例 ({', '.join(i.get('displayName','?') for i in targets)})"

    detail = ", ".join(i.get("displayName", "?") for i in targets) or "无"
    return False, f"期望 {EXPECTED_COUNT} 个 {NAME_PREFIX}* 实例，实际 {len(targets)} 个 ({detail})"


# ---------------------------------------------------------------- 进程操作


def find_processes():
    """返回匹配 PROCESS_PATTERN 的 PID 列表。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", PROCESS_PATTERN],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("pgrep 执行失败: %s", e)
        return []

    pids = []
    for line in out.split():
        try:
            pids.append(int(line))
        except ValueError:
            pass
    return pids


def dump_thread_stacks(pids):
    """
    在 kill 之前抓一份线程栈留作现场。
    卡死的根因还没定位，这些栈是唯一的线索。
    失败不影响主流程。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = f"/Users/xulihang/Library/Logs/imagetrans-jstack/{stamp}"
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as e:
        log.warning("无法创建 %s: %s", outdir, e)
        return

    for pid in pids:
        try:
            with open(posixpath.join(outdir, f"{pid}.txt"), "w") as f:
                subprocess.run(["jstack", str(pid)], stdout=f, stderr=subprocess.STDOUT, timeout=30)
            log.info("已保存 %s 的线程栈", pid)
        except Exception as e:
            log.warning("抓取 %s 线程栈失败: %s", pid, e)


def kill_all(include_self_guard=True):
    """
    杀掉所有 ImageTrans 进程。返回被杀的 PID 列表。

    注意：PROCESS_PATTERN 也会匹配到本脚本自己（如果它被以含
    "ImageTrans.jar" 的命令行启动），所以先把自身 PID 排除掉。
    """
    pids = [p for p in find_processes() if p != os.getpid()]
    if not pids:
        log.info("没有找到运行中的 ImageTrans 进程")
        return []

    log.info("准备杀掉 %d 个进程: %s", len(pids), pids)
    dump_thread_stacks(pids)

    # 先 TERM
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as e:
            log.warning("发送 SIGTERM 给 %s 失败: %s", pid, e)

    # 等待退出，超时则 KILL
    deadline = time.time() + 10
    while time.time() < deadline:
        if not [p for p in find_processes() if p != os.getpid()]:
            log.info("所有进程已退出")
            return pids
        time.sleep(0.5)

    remaining = [p for p in find_processes() if p != os.getpid()]
    if remaining:
        log.warning("%d 个进程未响应 SIGTERM，强制 KILL: %s", len(remaining), remaining)
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as e:
                log.warning("发送 SIGKILL 给 %s 失败: %s", pid, e)

    return pids


def instance_dir(app_path):
    """返回某个 app bundle 内部 ImageTrans 的安装目录。"""
    return posixpath.join(app_path, RESOURCES_SUBPATH)


def resolve_java(app_path):
    """
    找到该 bundle 内置的 java。
    对应 Server.bas:825-837：优先用 jdk-23，找不到才退回系统 java。
    """
    bundled = posixpath.join(
        instance_dir(app_path), "jdk-23", "Contents", "Home", "bin", "java"
    )
    if os.path.exists(bundled):
        return bundled

    log.warning("未找到内置 java (%s)，退回系统 java", bundled)
    return "java"


def resolve_jar(app_path):
    """返回该 bundle 内 ImageTrans.jar 的完整路径。"""
    return posixpath.join(instance_dir(app_path), "ImageTrans.jar")


def build_jvm_params(app_path):
    """
    复现 Server.bas:826-839 的 JVM 参数。
    libPath 对应 Server.bas:832 的 File.DirApp/jdk-23/Contents/Home/javafx/lib
    """
    lib_path = posixpath.join(
        instance_dir(app_path), "jdk-23", "Contents", "Home", "javafx", "lib"
    )
    return [
        "--module-path", lib_path,
        "--add-modules",
        "javafx.base,javafx.controls,javafx.graphics,javafx.web,javafx.swing",
        "--add-opens",
        "javafx.controls/com.sun.javafx.scene.control.skin=ALL-UNNAMED",
        "--add-exports", "javafx.base/com.sun.javafx.collections=ALL-UNNAMED",
        "--add-exports", "java.desktop/sun.awt=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.plugins.jpeg=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.plugins.png=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.plugins.bmp=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.plugins.gif=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.plugins.wbmp=ALL-UNNAMED",
        "--add-exports", "java.desktop/com.sun.imageio.spi=ALL-UNNAMED",
        "--add-opens", "java.desktop/com.sun.imageio.plugins.jpeg=ALL-UNNAMED",
    ]


def start_instance(app_path, project_path):
    """
    启动一个实例。

    复现 Server.bas:840-850 的命令行：
        java <jvmParams> -jar ImageTrans.jar <项目路径> server

    两个位置参数都不能少（见 ImageTrans.b4j:908-913）：
      - Args(0) = 项目路径，用于 openProject
      - Args(1) = "server"，用于进入 cliMode

    不用 `open <app>.app`：open 无法传位置参数，且 app 已在运行时会
    忽略 --args 只做激活，静默失效。

    失败时抛 RuntimeError（携带可归因的信息），由调用方决定如何降级。
    """
    java = resolve_java(app_path)
    jar = resolve_jar(app_path)
    workdir = instance_dir(app_path)

    if not os.path.exists(jar):
        raise RuntimeError(f"ImageTrans.jar 不存在: {jar}")
    if not os.path.exists(project_path):
        raise RuntimeError(f"项目目录不存在: {project_path}")
    if not os.path.exists(java):
        raise RuntimeError(f"java 不存在: {java}")

    cmd = [java] + build_jvm_params(app_path) + ["-jar", jar, project_path, "server"]
    log.info("启动: %s", " ".join(cmd))

    try:
        # 脱离父进程，避免脚本退出时连带杀掉实例
        with open(posixpath.join("/tmp", "imagetrans_watchdog_start.log"), "a") as out:
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as e:
        raise RuntimeError(f"启动失败 ({app_path}): {e}") from e

    log.info("已启动 %s (pid=%d)，项目目录 %s", app_path, proc.pid, project_path)
    return proc


def restart_all():
    """
    杀掉所有进程，然后依次启动三个实例。

    启动后校验：等 STARTUP_VERIFY_DELAY 秒再查一次 /list，确认实例真的连上了。
    只看 Popen 成功是不够的 —— 进程起来但没连上服务端，是静默失败。

    返回 (启动成功数, 启动后 /list 中的实例数 或 None)。
    抛出 RuntimeError 表示配置类错误（不应继续重启）。
    """
    kill_all()

    log.info("等待 %d 秒让端口和文件锁释放", KILL_GRACE_SECONDS)
    time.sleep(KILL_GRACE_SECONDS)

    started = 0
    for i, (app_path, project_path) in enumerate(zip(APP_PATHS, PROJECT_PATHS)):
        if i > 0:
            time.sleep(START_INTERVAL_SECONDS)
        try:
            start_instance(app_path, project_path)
            started += 1
        except RuntimeError as e:
            # 配置类错误：再重启也没用，向上抛出让主循环停止
            raise RuntimeError(f"{e}（请检查 APP_PATHS / PROJECT_PATHS 配置）") from e

    log.info("已发起启动: %d/%d，等待 %d 秒后校验连接情况",
             started, len(APP_PATHS), STARTUP_VERIFY_DELAY)
    time.sleep(STARTUP_VERIFY_DELAY)

    actual = None
    try:
        actual = len(target_instances(fetch_instances()))
    except Exception as e:
        log.warning("启动后校验失败（不影响已启动的进程）: %s", e)

    if actual is not None:
        if actual >= EXPECTED_COUNT:
            log.info("校验通过: /list 中有 %d 个 %s* 实例", actual, NAME_PREFIX)
        elif started >= len(APP_PATHS):
            log.warning(
                "已启动全部 %d 个进程，但 /list 中只有 %d 个 %s* 实例。"
                "可能是 PROJECT_PATHS 与预期的 displayName 不匹配。",
                started, actual, NAME_PREFIX,
            )

    return started, actual


# ---------------------------------------------------------------- 主循环


def run_once(dry_run=False):
    """执行一次检查。返回 True 表示健康。"""
    try:
        healthy, detail = check()
    except Exception as e:
        log.warning("检查失败: %s", e)
        return None

    if healthy:
        log.info(detail)
        return True

    log.warning(detail)
    if dry_run:
        log.info("[dry-run] 跳过重启")
        return False

    restart_all()
    return False


def main():
    parser = argparse.ArgumentParser(description="ImageTrans 客户端看门狗")
    parser.add_argument("--once", action="store_true", help="只检查一次")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不执行 kill/start")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL, help="检查间隔（秒）")
    args = parser.parse_args()

    setup_logging()

    if args.once or args.dry_run:
        result = run_once(dry_run=args.dry_run)
        sys.exit(0 if result else 1)

    log.info(
        "看门狗启动: 每 %d 秒检查 %s，期望 %d 个 %s* 实例（连续 %d 次异常才重启）",
        args.interval, SERVER_URL, EXPECTED_COUNT, NAME_PREFIX, FAILURE_THRESHOLD,
    )

    consecutive_failures = 0      # health 为 False 的连续次数
    network_failures = 0          # 网络异常的连续次数
    config_errors = 0             # 配置类错误的连续次数
    last_restart = time.time()    # 首次失败不重启，避免服务端启动中误杀

    while True:
        status = "healthy"  # healthy / unhealthy / network_error

        try:
            instances = fetch_instances()
        except Exception as e:
            # 网络/服务端问题：不做任何重启动作。
            # 客户端可能是好的，重启解决不了服务端故障。
            network_failures += 1
            consecutive_failures += 1
            status = "network_error"
            log.warning(
                "网络检查失败（连续第 %d 次，需 %d 次才认为不可用）: %s",
                network_failures, NETWORK_FAILURE_THRESHOLD, e,
            )
            if network_failures >= NETWORK_FAILURE_THRESHOLD:
                log.error("服务端持续不可达，等待恢复（不触发重启）")
        else:
            network_failures = 0
            targets = target_instances(instances)
            if len(targets) == EXPECTED_COUNT:
                if consecutive_failures:
                    log.info("恢复正常")
                consecutive_failures = 0
                config_errors = 0
                names = ", ".join(i.get("displayName", "?") for i in targets)
                log.info("ok: %d 个实例 (%s)", len(targets), names)
            else:
                consecutive_failures += 1
                detail = ", ".join(i.get("displayName", "?") for i in targets) or "无"
                log.warning(
                    "期望 %d 个 %s* 实例，实际 %d 个 (%s)（连续第 %d 次）",
                    EXPECTED_COUNT, NAME_PREFIX, len(targets), detail, consecutive_failures,
                )
                status = "unhealthy"

        if status == "unhealthy" and consecutive_failures >= FAILURE_THRESHOLD:
            since_restart = time.time() - last_restart
            if since_restart < RESTART_COOLDOWN:
                log.warning(
                    "处于冷静期（距上次重启 %.0f 秒，需 %d 秒），跳过本次重启",
                    since_restart, RESTART_COOLDOWN,
                )
            else:
                try:
                    restart_all()
                    config_errors = 0
                except RuntimeError as e:
                    # 配置错误：重启解决不了，停下来等人处理，避免空转
                    config_errors += 1
                    log.error("重启失败：%s", e)
                    if config_errors >= CONFIG_ERROR_THRESHOLD:
                        log.error(
                            "连续 %d 次配置类错误，看门狗停止自动重启。"
                            "请修正配置后重新启动看门狗。",
                            config_errors,
                        )
                        return

                last_restart = time.time()
                consecutive_failures = 0

        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("看门狗退出")
