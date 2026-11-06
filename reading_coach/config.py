# -*- coding: utf-8 -*-
"""运行契约：全项目唯一的配置、路径与数据库入口来源。

三个进程（cli / web / scheduler）原本各自计算 READING_COACH_HOME 与端口，
导致同一份配置出现四个真值来源。本模块把环境变量翻译成值，只暴露纯函数与
两个数据库工厂。它不导入任何业务模块，因此可被任意层依赖而不产生环。

部署约定：默认绑定回环地址且不启用认证；一旦绑定非回环地址，必须同时设置
READING_COACH_TOKEN，否则拒绝启动。这条约束写在 require_token() 里，是防止
把无认证的本地工作台直接暴露到公网的唯一门禁。
"""
from __future__ import annotations

# - [INPUT]: 依赖标准库 logging / os / sqlite3 / sys / pathlib；可选依赖 python-dotenv 的 load_dotenv。
# - [OUTPUT]: 对外提供 load_env、data_home、port、bind_host、access_token、allowed_hosts、
#   require_token、connect、init_db、setup_logging、is_loopback、PROJECT_ROOT、DEFAULT_PORT。
# - [POS]: 运行契约层。cli / web / scheduler / session 都从此处取值，任何新增配置只在此落地。
# - [PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md

import logging
import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8765
LOOPBACK = "127.0.0.1"
DB_NAME = "coach.db"
BUSY_TIMEOUT_MS = 30_000

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def env_file() -> Path | None:
    """返回要读取的 .env 路径。显式变量 > 当前目录 > 项目根（开发模式）。"""
    explicit = os.environ.get("READING_COACH_ENV_FILE")
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
        if candidate.is_file():
            return candidate
    return None


def load_env() -> Path | None:
    """把 .env 读进进程。进程已有的环境变量优先，文件不覆盖它。

    容器与 systemd 通过真实环境变量注入，因此 dotenv 缺失是正常情况，不是错误。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    path = env_file()
    if path is not None:
        load_dotenv(path, override=False)
    return path


# ---------------------------------------------------------------------------
# Paths and network
# ---------------------------------------------------------------------------

def data_home() -> Path:
    """返回数据根目录。所有 coach.db、notes/、highlights/、日志都落在它下面。"""
    raw = os.environ.get("READING_COACH_HOME")
    return Path(raw).expanduser().resolve() if raw else PROJECT_ROOT / "data"


def port() -> int:
    return int(os.environ.get("READING_COACH_PORT", DEFAULT_PORT))


def bind_host() -> str:
    return os.environ.get("READING_COACH_BIND", LOOPBACK).strip()


def is_loopback(host: str) -> bool:
    return host in (LOOPBACK, "localhost", "::1")


def access_token() -> str | None:
    return os.environ.get("READING_COACH_TOKEN") or None


def allowed_hosts(listen_port: int) -> set[str]:
    """返回 Host / Origin 白名单。

    回环地址永远在白名单内。反向代理场景用 READING_COACH_ALLOWED_HOSTS 追加
    逗号分隔的 host:port，绑定非回环地址时追加绑定地址本身。
    """
    hosts = {f"{LOOPBACK}:{listen_port}", f"localhost:{listen_port}"}
    host = bind_host()
    if not is_loopback(host):
        hosts.add(f"{host}:{listen_port}")
    extra = os.environ.get("READING_COACH_ALLOWED_HOSTS", "")
    hosts.update(h.strip() for h in extra.split(",") if h.strip())
    return hosts


def require_token() -> None:
    """非回环绑定而没有访问令牌时拒绝启动。任何入口在监听前都必须调用。

    READING_COACH_TRUST_PROXY=1 是唯一例外：容器必须监听 0.0.0.0 才能接受端口映射，
    而宿主机把端口绑在 127.0.0.1。设置它等于声明"我确认这个监听地址只在受信任的
    回环边界内"；公网部署不得设置，必须改用令牌。
    """
    if is_loopback(bind_host()):
        return
    if os.environ.get("READING_COACH_TRUST_PROXY") == "1":
        return
    if access_token() is None:
        raise RuntimeError(
            "READING_COACH_BIND 绑定了非回环地址，必须同时设置 READING_COACH_TOKEN；"
            "工作台没有会话认证，裸绑公网会泄露模型密钥与阅读记录。"
        )


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect(home: Path) -> sqlite3.Connection:
    """打开 coach.db。统一超时与 busy_timeout，避免 web 与 scheduler 抢写时直接报错。"""
    home.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home / DB_NAME, timeout=BUSY_TIMEOUT_MS / 1000)
    db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return db


def init_db(home: Path) -> None:
    """启用 WAL。web 与 scheduler 是两个进程，WAL 让读写不再互相阻塞。"""
    db = connect(home)
    try:
        db.execute("PRAGMA journal_mode=WAL")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    """统一日志格式。默认 INFO，用 READING_COACH_LOG_LEVEL 覆盖。"""
    level = os.environ.get("READING_COACH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
