#!/usr/bin/env python3
"""t.bt.cn 三项目远程部署脚本（SFTP / FTP / FTPS）。

流程：本地构建（go build / composer / npm）→ 按映射上传产物 → 可选执行远程命令（仅 SFTP）。

用法：
    python3 deploy.py --config deploy.toml all              # 部署全部
    python3 deploy.py --config deploy.toml master frontend  # 只部署指定项目
    python3 deploy.py --config deploy.toml --dry-run all    # 只打印将执行的动作
    python3 deploy.py --config deploy.toml --skip-build web # 跳过构建直接上传现有产物

配置模板见同目录 deploy.example.toml；正式配置 deploy.toml 含密码，已在 .gitignore 中忽略。
依赖：Python >= 3.11（tomllib）；SFTP 协议需 `pip install paramiko`，FTP/FTPS 零第三方依赖。
"""

from __future__ import annotations

import argparse
import fnmatch
import ftplib
import os
import posixpath
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("需要 Python >= 3.11（内置 tomllib）；当前版本过低。")

# 无论配置写什么，这些模式永远排除：版本库/本地敏感配置绝不上传
ALWAYS_EXCLUDE = [".git", ".git/*", "*/.git/*", ".env", "*/.env", ".env.*", "*/.env.*"]


def log(msg: str) -> None:
    """打印带时间戳的进度日志。

    @param msg 日志内容
    @return None
    """
    print(f"[deploy {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> NoReturn:
    """打印错误并以非零码退出。

    @param msg 错误内容
    @return 不返回（进程退出）
    """
    print(f"[deploy] 错误：{msg}", file=sys.stderr)
    sys.exit(1)


def resolve_password(server: dict) -> str:
    """解析服务器密码：优先 password_env 指向的环境变量，其次明文 password。

    @param server 配置中的 [server] 段
    @return 密码字符串（可为空，SFTP 密钥登录时允许）
    """
    env_name = server.get("password_env", "")
    if env_name:
        value = os.environ.get(env_name)
        if value is None:
            die(f"password_env 指定的环境变量 {env_name} 未设置")
        return value
    return server.get("password", "")


class SftpTransport:
    """SFTP 传输通道（paramiko），支持上传与远程命令执行。"""

    def __init__(self, server: dict):
        """建立 SSH + SFTP 连接。

        @param server [server] 配置段（host/port/username/password|key_file）
        """
        try:
            import paramiko
        except ModuleNotFoundError:
            die("SFTP 协议需要 paramiko：pip install paramiko")
            raise  # 不可达，仅供类型收窄
        self._ssh = paramiko.SSHClient()
        # 部署目标为内网/自有服务器，首次连接自动记录 host key；如需严格校验请预置 known_hosts
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: dict = {
            "hostname": server["host"],
            "port": int(server.get("port", 22)),
            "username": server["username"],
            "timeout": int(server.get("timeout", 15)),
        }
        key_file = server.get("key_file", "")
        password = resolve_password(server)
        # 默认关闭 paramiko 的自动密钥探测：不开则它会在密码登录前，
        # 把 ssh-agent（如 1Password SSH agent）里挂载的每一把私钥都拿去对服务器试登录，
        # 表现为对服务器的批量登录尝试（易被当作爆破触发 fail2ban），也会弹出一堆 1Password 授权请求。
        # 只有配置显式开启 use_ssh_agent 时才允许走 agent/known-keys 探测。
        kwargs["allow_agent"] = bool(server.get("use_ssh_agent", False))
        kwargs["look_for_keys"] = bool(server.get("use_ssh_agent", False))
        if key_file:
            kwargs["key_filename"] = str(Path(key_file).expanduser())
            if password:
                kwargs["passphrase"] = password
        else:
            kwargs["password"] = password
        self._ssh.connect(**kwargs)
        self._sftp = self._ssh.open_sftp()
        self._dir_cache: set[str] = set()

    def ensure_dir(self, remote_dir: str) -> None:
        """逐级创建远程目录（幂等，带缓存避免重复 stat）。

        @param remote_dir 远程绝对目录
        @return None
        """
        if remote_dir in self._dir_cache or remote_dir in ("/", ""):
            return
        parent = posixpath.dirname(remote_dir.rstrip("/"))
        self.ensure_dir(parent)
        try:
            self._sftp.stat(remote_dir)
        except FileNotFoundError:
            self._sftp.mkdir(remote_dir)
        self._dir_cache.add(remote_dir)

    def upload_file(self, local: Path, remote: str) -> None:
        """上传单文件：先传临时名再原子 rename，避免替换运行中的二进制/半截文件被读走。

        @param local  本地文件路径
        @param remote 远程目标绝对路径
        @return None
        """
        self.ensure_dir(posixpath.dirname(remote))
        tmp = remote + ".uploading"
        self._sftp.put(str(local), tmp)
        # posix rename 原子替换；paramiko 的 rename 对已存在目标会失败，故先删
        try:
            self._sftp.remove(remote)
        except FileNotFoundError:
            pass
        self._sftp.rename(tmp, remote)
        # 保留本地可执行位（Go 二进制场景）
        if os.access(local, os.X_OK):
            self._sftp.chmod(remote, 0o755)

    def exec_command(self, command: str) -> int:
        """执行远程命令并回显输出。

        @param command 远程 shell 命令
        @return 命令退出码
        """
        _, stdout, stderr = self._ssh.exec_command(command)
        code = stdout.channel.recv_exit_status()
        out, err = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
        if out.strip():
            log(f"  远程输出: {out.strip()}")
        if err.strip():
            log(f"  远程错误输出: {err.strip()}")
        return code

    def close(self) -> None:
        """关闭 SFTP 与 SSH 连接。

        @return None
        """
        self._sftp.close()
        self._ssh.close()


class FtpTransport:
    """FTP / FTPS 传输通道（标准库 ftplib），不支持远程命令。"""

    def __init__(self, server: dict, use_tls: bool):
        """建立 FTP(S) 连接并登录。

        @param server  [server] 配置段
        @param use_tls True 走 FTPS（显式 TLS + PROT P）
        """
        cls = ftplib.FTP_TLS if use_tls else ftplib.FTP
        self._ftp = cls()
        self._ftp.connect(server["host"], int(server.get("port", 21)), timeout=int(server.get("timeout", 15)))
        self._ftp.login(server["username"], resolve_password(server))
        if isinstance(self._ftp, ftplib.FTP_TLS):
            self._ftp.prot_p()  # 数据通道也加密
        self._ftp.set_pasv(bool(server.get("passive", True)))
        self._dir_cache: set[str] = set()

    def ensure_dir(self, remote_dir: str) -> None:
        """逐级 MKD 远程目录（已存在的错误忽略，幂等）。

        @param remote_dir 远程绝对目录
        @return None
        """
        if remote_dir in self._dir_cache or remote_dir in ("/", ""):
            return
        self.ensure_dir(posixpath.dirname(remote_dir.rstrip("/")))
        try:
            self._ftp.mkd(remote_dir)
        except ftplib.error_perm as exc:
            # 550 目录已存在属预期；其他权限错误如实抛出
            if not str(exc).startswith("550"):
                raise
        self._dir_cache.add(remote_dir)

    def upload_file(self, local: Path, remote: str) -> None:
        """上传单文件（临时名 + RNFR/RNTO 原子替换）。

        @param local  本地文件路径
        @param remote 远程目标绝对路径
        @return None
        """
        self.ensure_dir(posixpath.dirname(remote))
        tmp = remote + ".uploading"
        with open(local, "rb") as fh:
            self._ftp.storbinary(f"STOR {tmp}", fh)
        try:
            self._ftp.delete(remote)
        except ftplib.error_perm:
            pass  # 目标不存在
        self._ftp.rename(tmp, remote)

    def exec_command(self, command: str) -> int:
        """FTP 无法执行远程命令，提示改用 SFTP。

        @param command 远程命令（仅用于提示）
        @return 恒为 1（失败）
        """
        log(f"  警告：FTP 协议无法执行远程命令，已跳过: {command}")
        return 1

    def close(self) -> None:
        """关闭 FTP 连接。

        @return None
        """
        try:
            self._ftp.quit()
        except Exception:
            self._ftp.close()


def open_transport(server: dict):
    """按协议建立传输通道。

    @param server [server] 配置段，protocol ∈ sftp|ftp|ftps
    @return SftpTransport 或 FtpTransport 实例
    """
    protocol = server.get("protocol", "sftp").lower()
    if protocol == "sftp":
        return SftpTransport(server)
    if protocol in ("ftp", "ftps"):
        return FtpTransport(server, use_tls=(protocol == "ftps"))
    die(f"不支持的协议: {protocol}（可选 sftp / ftp / ftps）")


def is_excluded(rel: str, patterns: list[str]) -> bool:
    """判断相对路径是否命中排除模式（对路径本身及其任一父目录做 fnmatch）。

    @param rel      正斜杠相对路径
    @param patterns 排除模式列表（fnmatch 语法）
    @return 命中返回 True
    """
    parts = rel.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        # "tests" 这类裸目录名应排除整棵子树
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def collect_files(local_root: Path, excludes: list[str]) -> list[tuple[Path, str]]:
    """递归收集目录下待上传文件。

    @param local_root 本地目录（或单文件）
    @param excludes   排除模式
    @return [(本地绝对路径, 相对路径)] 列表；local_root 为文件时相对路径为其文件名
    """
    patterns = list(excludes) + ALWAYS_EXCLUDE
    if local_root.is_file():
        return [(local_root, local_root.name)]
    result: list[tuple[Path, str]] = []
    for path in sorted(local_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_root).as_posix()
        if is_excluded(rel, patterns):
            continue
        result.append((path, rel))
    return result


def run_build(name: str, source: Path, commands: list[str], dry_run: bool) -> None:
    """在项目源码目录顺序执行构建命令，任一失败即中止。

    @param name     项目名（日志用）
    @param source   项目源码目录
    @param commands 构建命令列表
    @param dry_run  True 时只打印不执行
    @return None
    """
    for cmd in commands:
        log(f"[{name}] 构建: {cmd}")
        if dry_run:
            continue
        # shell=True 是刻意的：命令来自操作者本人的 deploy.toml（与 Makefile 同信任级），
        # 且构建命令需要 && / 环境变量等 shell 语法；本脚本不接受任何不可信输入拼接命令。
        proc = subprocess.run(cmd, shell=True, cwd=source)
        if proc.returncode != 0:
            die(f"[{name}] 构建命令失败（exit {proc.returncode}）: {cmd}")


def deploy_project(name: str, project: dict, base: Path, transport, dry_run: bool, skip_build: bool) -> None:
    """部署单个项目：构建 → 按 uploads 映射上传 → post_commands。

    @param name       项目名
    @param project    [projects.<name>] 配置段
    @param base       配置文件所在目录（source 相对路径的解析基准）
    @param transport  已连接的传输通道（dry_run 时为 None）
    @param dry_run    只打印动作
    @param skip_build 跳过本地构建
    @return None
    """
    raw_source = Path(project["source"]).expanduser()
    # source 相对路径以配置文件目录为基准，配置可在任意 CWD 下执行
    source = (raw_source if raw_source.is_absolute() else base / raw_source).resolve()
    if not source.exists():
        die(f"[{name}] source 不存在: {source}")

    if not skip_build:
        run_build(name, source, project.get("build", []), dry_run)

    total = 0
    for mapping in project.get("uploads", []):
        local = (source / mapping["local"]).resolve()
        remote_root = mapping["remote"].rstrip("/")
        if not local.exists():
            if dry_run:
                # dry-run 未真正构建，产物缺失属预期，降级为提示
                log(f"[{name}] 提示：{local} 尚不存在（构建后生成）→ {remote_root}")
                continue
            die(f"[{name}] 上传源不存在（构建产物缺失？）: {local}")
        files = collect_files(local, mapping.get("exclude", []))
        log(f"[{name}] {local} → {remote_root}（{len(files)} 个文件）")
        for local_file, rel in files:
            remote = f"{remote_root}/{rel}"
            if dry_run:
                print(f"    would upload {rel}")
                continue
            transport.upload_file(local_file, remote)
        total += len(files)
    log(f"[{name}] 上传完成，共 {total} 个文件")

    for cmd in project.get("post_commands", []):
        log(f"[{name}] 远程命令: {cmd}")
        if dry_run:
            continue
        code = transport.exec_command(cmd)
        if code != 0:
            die(f"[{name}] 远程命令失败（exit {code}）: {cmd}")


def main() -> None:
    """解析参数、加载配置并按序部署所选项目。

    @return None
    """
    parser = argparse.ArgumentParser(description="t.bt.cn 三项目远程部署（SFTP/FTP/FTPS）")
    parser.add_argument("targets", nargs="+", help="项目名（配置中 projects.* 的键）或 all")
    parser.add_argument("--config", default=str(Path(__file__).parent / "deploy.toml"), help="TOML 配置路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的构建/上传/命令，不实际连接")
    parser.add_argument("--skip-build", action="store_true", help="跳过本地构建，直接上传现有产物")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        die(f"配置文件不存在: {config_path}（参考 deploy.example.toml 复制一份）")
    with open(config_path, "rb") as fh:
        config = tomllib.load(fh)

    server = config.get("server") or die("配置缺少 [server] 段")
    projects: dict = config.get("projects") or die("配置缺少 [projects.*] 段")

    names = list(projects) if args.targets == ["all"] else args.targets
    unknown = [n for n in names if n not in projects]
    if unknown:
        die(f"未知项目 {unknown}，可选: {list(projects)} 或 all")

    log(f"目标服务器: {server.get('protocol', 'sftp')}://{server['username']}@{server['host']}"
        f":{server.get('port', '')} ｜ 项目: {', '.join(names)}{'（dry-run）' if args.dry_run else ''}")

    transport = None if args.dry_run else open_transport(server)
    try:
        for name in names:
            deploy_project(name, projects[name], config_path.parent.resolve(),
                           transport, args.dry_run, args.skip_build)
    finally:
        if transport is not None:
            transport.close()
    log("全部完成")


if __name__ == "__main__":
    main()
