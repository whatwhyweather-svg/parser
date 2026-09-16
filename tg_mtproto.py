"""Общий MTProto-клиент: IPv4/IPv6, WARP, без сброса DC сессии."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

import tg_ssl  # noqa: F401  — до telethon

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.errors import AuthKeyNotFound, InvalidBufferError
from telethon.network.connection import (
    ConnectionHttp,
    ConnectionTcpAbridged,
    ConnectionTcpFull,
    ConnectionTcpIntermediate,
    ConnectionTcpObfuscated,
)
from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyAbridged,
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.sessions import SQLiteSession

from tg_user_source import (
    auto_mtproto_proxy_url,
    parse_proxy_spec,
    _parse_proxy,
)

DC_IPV4 = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}
DC_IPV6 = {
    1: "2001:b28:f23d:f001::a",
    2: "2001:67c:4e8:f002::a",
    3: "2001:b28:f23d:f001::c",
    4: "2001:67c:4e8:f002::b",
    5: "2001:b28:f23f:f005::a",
}

_WARP_CLI = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "Cloudflare",
    "Cloudflare WARP",
    "warp-cli.exe",
)
_SOCKS_PORTS = (40000, 40001, 1080, 10808)

LogFn = Callable[[str], None] | None

# WARP MASQUE часто «съедает» один DC и пропускает другой — сначала Obfuscated по всем DC.
_MODES_WARP: tuple[dict[str, Any], ...] = (
    {"use_ipv6": True, "connection": ConnectionTcpObfuscated, "timeout": 12},
    {"use_ipv6": True, "connection": ConnectionTcpAbridged, "timeout": 10},
    {"use_ipv6": True, "connection": ConnectionTcpIntermediate, "timeout": 10},
    {"use_ipv6": False, "connection": ConnectionTcpObfuscated, "timeout": 8},
)
# WARP в режиме SOCKS (Habr / Cloudflare Local Proxy): IPv4 + TcpFull.
_MODES_SOCKS: tuple[dict[str, Any], ...] = (
    {"use_ipv6": False, "connection": ConnectionTcpFull, "timeout": 22},
    {"use_ipv6": False, "connection": ConnectionTcpObfuscated, "timeout": 16},
    {"use_ipv6": False, "connection": ConnectionTcpAbridged, "timeout": 16},
    {"use_ipv6": False, "connection": ConnectionHttp, "timeout": 18},
)
_MODES_V6: tuple[dict[str, Any], ...] = (
    {"use_ipv6": True, "connection": ConnectionHttp, "timeout": 18},
    {"use_ipv6": True, "connection": ConnectionTcpObfuscated, "timeout": 16},
    {"use_ipv6": True, "connection": ConnectionTcpAbridged, "timeout": 16},
    {"use_ipv6": True, "connection": ConnectionTcpFull, "timeout": 14},
    {"use_ipv6": False, "connection": ConnectionTcpObfuscated, "timeout": 10},
)
_MODES_V4: tuple[dict[str, Any], ...] = (
    {"use_ipv6": False, "connection": ConnectionTcpObfuscated, "timeout": 22},
    {"use_ipv6": False, "connection": ConnectionTcpAbridged, "timeout": 20},
    {"use_ipv6": False, "connection": ConnectionTcpFull, "timeout": 18},
    {"use_ipv6": True, "connection": ConnectionTcpObfuscated, "timeout": 10},
)
_MODES_BOTH: tuple[dict[str, Any], ...] = (
    {"use_ipv6": True, "connection": ConnectionTcpObfuscated, "timeout": 14},
    {"use_ipv6": True, "connection": ConnectionTcpAbridged, "timeout": 14},
    {"use_ipv6": False, "connection": ConnectionTcpObfuscated, "timeout": 16},
    {"use_ipv6": False, "connection": ConnectionTcpAbridged, "timeout": 16},
)

_WARP_SOCKS_HINT = (
    "Не достучались до Telegram через WARP. Не включай Proxy в WARP. "
    "Подожди 10с и нажми вход ещё раз."
)


def _playbook(
    dc_id: int, *, proxy: bool, warp: bool
) -> list[tuple[bool, Any, int]]:
    """Живой прогон 31 авг 2026, WARP MASQUE: DC3 только IPv4 TcpFull;
    DC2/5 — IPv6 Obfuscated; IPv4 Obfuscated почти всегда мёртв."""
    if proxy:
        return [
            (False, ConnectionTcpFull, 22),
            (False, ConnectionTcpObfuscated, 16),
            (False, ConnectionTcpAbridged, 16),
        ]
    dc = int(dc_id or 2) or 2
    if dc == 3:
        return [
            (False, ConnectionTcpFull, 16),
            (False, ConnectionTcpAbridged, 12),
        ]
    if warp:
        return [
            (True, ConnectionTcpObfuscated, 16),
            (True, ConnectionTcpAbridged, 14),
            (False, ConnectionTcpFull, 14),
            (False, ConnectionTcpAbridged, 12),
        ]
    return [
        (True, ConnectionTcpObfuscated, 16),
        (False, ConnectionTcpObfuscated, 16),
        (True, ConnectionTcpFull, 14),
        (False, ConnectionTcpFull, 14),
    ]


class SessionDeadError(RuntimeError):
    """Файл сессии есть, но Telegram его больше не принимает."""


def _no_window() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)


def _port_open(host: str, port: int, wait: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=wait):
            return True
    except OSError:
        return False


def detect_local_socks() -> str | None:
    """Локальный SOCKS WARP (по умолчанию 127.0.0.1:40000)."""
    for port in _SOCKS_PORTS:
        if _port_open("127.0.0.1", port):
            return f"socks5://127.0.0.1:{port}"
    return None


def resolve_telegram_proxy(proxy_raw: str | None = None) -> str | None:
    raw = (proxy_raw or "").strip()
    if raw:
        return raw
    auto = auto_mtproto_proxy_url()
    if auto:
        return auto
    return detect_local_socks()


def warp_tunnel_on() -> bool:
    """Есть ли активный туннель Cloudflare WARP (не «только DNS 1.1.1.1»)."""
    if os.name != "nt":
        return False
    flags = _no_window()
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetAdapter | Where-Object { "
                "$_.Status -eq 'Up' -and "
                "($_.Name -match 'WARP' -or $_.InterfaceDescription -match 'Cloudflare') "
                "} | Select-Object -ExpandProperty Name",
            ],
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
        if (out or "").strip():
            return True
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["ipconfig"],
            timeout=8,
            text=True,
            encoding="oem",
            errors="replace",
            creationflags=flags,
        )
    except Exception:
        return False
    compact = (out or "").casefold().replace(" ", "")
    return "cloudflarewarp" in compact or "cloudflarewarp" in (out or "").casefold()


def warp_tunnel_label() -> str:
    """MASQUE (HTTP3) / WireGuard — для лога."""
    exe = _WARP_CLI
    if not os.path.isfile(exe):
        return "туннель"
    try:
        out = subprocess.check_output(
            [exe, "tunnel", "stats"],
            timeout=6,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_no_window(),
        )
    except Exception:
        return "туннель"
    text = out or ""
    if "MASQUE" in text:
        if "HTTP/2" in text:
            return "MASQUE HTTP/2"
        return "MASQUE HTTP/3"
    if "WireGuard" in text:
        return "WireGuard"
    return "туннель"


def _fmt_exc(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc).strip()
    if isinstance(exc, asyncio.TimeoutError) or name in {"TimeoutError", "Timeout"}:
        return "таймаут до серверов Telegram"
    if not text:
        return name
    return f"{name}: {text}"[:220]


def _is_auth_miss(err: BaseException | None) -> bool:
    if err is None:
        return False
    if isinstance(err, AuthKeyNotFound):
        return True
    if isinstance(err, InvalidBufferError) and getattr(err, "code", None) == 404:
        return True
    text = str(err).casefold()
    return "authorization key" in text or "authkeynotfound" in text


def _conn_name(connection) -> str:
    name = getattr(connection, "__name__", str(connection))
    return name.replace("ConnectionTcp", "").replace("Connection", "")


def _read_dc_key(session_path) -> tuple[int, AuthKey | None]:
    sess = SQLiteSession(str(session_path))
    try:
        dc = int(sess.dc_id or 2) or 2
        key = sess.auth_key
        return dc, key
    finally:
        sess.close()


def _unlink_session(session_path) -> None:
    p = Path(str(session_path))
    for f in p.parent.glob(p.name + "*"):
        try:
            f.unlink()
        except OSError:
            pass


def _apply_dc(
    sess: SQLiteSession,
    dc_id: int,
    use_ipv6: bool,
    auth_key: AuthKey | None,
    *,
    persist: bool = True,
) -> None:
    ip_map = DC_IPV6 if use_ipv6 else DC_IPV4
    dc = int(dc_id or 2) or 2
    ip = ip_map.get(dc) or ip_map[2]
    if persist:
        sess.set_dc(dc, ip, 443)
        if auth_key is not None:
            sess.auth_key = auth_key
        sess.save()
        return
    sess._dc_id = dc
    sess._server_address = ip
    sess._port = 443
    if auth_key is not None:
        sess._auth_key = auth_key


def _restore_dc(
    session_path,
    dc_id: int,
    auth_key: AuthKey | None,
    use_ipv6: bool,
) -> None:
    path = Path(str(session_path))
    if not path.exists() or auth_key is None:
        return
    sess = SQLiteSession(str(path))
    try:
        _apply_dc(sess, dc_id, use_ipv6, auth_key, persist=True)
    except Exception:
        pass
    finally:
        try:
            sess.close()
        except Exception:
            pass


def _pin_dc(
    client: TelegramClient,
    dc_id: int,
    use_ipv6: bool,
    auth_key: AuthKey | None,
    *,
    persist: bool = True,
) -> None:
    """Telethon иначе сбрасывает DC на 2 при смене IPv4/IPv6."""
    _apply_dc(client.session, dc_id, use_ipv6, auth_key, persist=persist)
    if auth_key is not None:
        sender = getattr(client, "_sender", None)
        if sender is not None:
            sender.auth_key = auth_key
            state = getattr(sender, "_state", None)
            if state is not None:
                state.auth_key = auth_key


def _client(
    session_path,
    api_id: int,
    api_hash: str,
    *,
    proxy_raw: str | None,
    connection_retries: int,
    retry_delay: int,
    timeout: int,
    use_ipv6: bool,
    connection,
    dc_id: int | None = None,
    auth_key: AuthKey | None = None,
    persist: bool = True,
) -> TelegramClient:
    """Сначала выставляем DC и семейство IP — иначе Telethon пишет DC2 в файл."""
    sess = SQLiteSession(str(session_path))
    try:
        dc = int(dc_id or sess.dc_id or 2) or 2
        key = auth_key if auth_key is not None else sess.auth_key
        _apply_dc(sess, dc, use_ipv6, key, persist=persist)
        spec = parse_proxy_spec(proxy_raw)
        conn = connection
        proxy_arg = _parse_proxy(proxy_raw)
        if spec and spec.kind == "mtproto":
            conn = ConnectionTcpMTProxyRandomizedIntermediate
            proxy_arg = (spec.mt_host, int(spec.mt_port), spec.mt_secret)
            use_ipv6 = False
        client = TelegramClient(
            sess,
            int(api_id),
            api_hash,
            connection=conn,
            use_ipv6=use_ipv6,
            proxy=proxy_arg,
            connection_retries=connection_retries,
            retry_delay=retry_delay,
            timeout=timeout,
        )
    except Exception:
        try:
            sess.close()
        except Exception:
            pass
        raise
    try:
        _pin_dc(client, dc, use_ipv6, key, persist=persist)
    except Exception:
        pass
    return client


def _dc_candidates(saved_dc: int, *, has_key: bool) -> list[int]:
    """
    С живым auth key ходим ТОЛЬКО на свой DC.
    Чужой DC отвечает «ключ неизвестен» → раньше это считалось
    «сессия слетела» и файл сносили — отсюда постоянный повторный вход.
    """
    return [int(saved_dc or 2) or 2]


def _ordered_dcs(saved_dc: int, alive: set[int], fallback: list[int]) -> list[int]:
    saved = int(saved_dc or 2) or 2
    out: list[int] = []
    if saved in alive:
        out.append(saved)
    for dc in (*sorted(alive), *fallback):
        if dc not in out and 1 <= dc <= 5:
            out.append(dc)
    if saved not in out:
        out.append(saved)
    return out or fallback[:1]


async def _probe_dcs() -> tuple[set[int], set[int]]:
    """Какие DC отвечают по TCP 443 — IPv4 и IPv6."""

    async def check(dc: int, ipv6: bool) -> tuple[int, bool, bool]:
        host = (DC_IPV6 if ipv6 else DC_IPV4)[dc]
        return dc, ipv6, await _tcp_ok(host, 443, 3.2)

    jobs = [check(dc, False) for dc in DC_IPV4] + [check(dc, True) for dc in DC_IPV6]
    v4: set[int] = set()
    v6: set[int] = set()
    for dc, ipv6, ok in await asyncio.gather(*jobs):
        if ok:
            (v6 if ipv6 else v4).add(dc)
    return v4, v6


def make_telegram_client(
    session_path,
    api_id: int,
    api_hash: str,
    *,
    proxy_raw: str | None = None,
    connection_retries: int = 3,
    retry_delay: int = 1,
    timeout: int = 15,
) -> TelegramClient:
    proxy = resolve_telegram_proxy(proxy_raw)
    dc_id, auth_key = 2, None
    try:
        dc_id, auth_key = _read_dc_key(session_path)
    except Exception:
        pass
    warp = warp_tunnel_on()
    book = _playbook(dc_id, proxy=bool(proxy), warp=warp)
    use_v6, conn, to = book[0]
    return _client(
        session_path,
        api_id,
        api_hash,
        proxy_raw=proxy,
        connection_retries=connection_retries,
        retry_delay=retry_delay,
        timeout=max(timeout, to),
        use_ipv6=use_v6,
        connection=conn,
        dc_id=dc_id,
        auth_key=auth_key,
    )


async def _tcp_ok(host: str, port: int = 443, wait: float = 5.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=wait,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def connect_telegram_client(
    session_path,
    api_id: int,
    api_hash: str,
    *,
    proxy_raw: str | None = None,
    timeout: int = 15,
    on_log: LogFn = None,
    roam_dcs: bool = True,
    auth_miss_kills: bool = True,
) -> TelegramClient:
    """Подключиться. Режим TCP зависит от DC: DC3 через WARP = IPv4 TcpFull."""

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    proxy = resolve_telegram_proxy(proxy_raw)
    warp = warp_tunnel_on()
    dc_id, auth_key = 2, None
    try:
        dc_id, auth_key = _read_dc_key(session_path)
    except Exception:
        pass
    orig_dc, orig_key = dc_id, auth_key

    if proxy:
        log(f"прокси {proxy} — MTProto через SOCKS")
    elif warp:
        log(f"WARP {warp_tunnel_label()} — DC{dc_id}, режим под этот DC")
    else:
        log(f"WARP нет · сессия DC{dc_id}")

    last: BaseException | None = None
    auth_miss_home = False
    home_dc = int(dc_id or 2) or 2
    if roam_dcs and auth_key is None:
        pending_dcs = _dc_candidates(dc_id, has_key=False)
    else:
        # С ключом — только домашний DC (roam_dcs игнорируем)
        pending_dcs = [home_dc]
    log(f"сессия DC{home_dc} · порядок {','.join('DC'+str(x) for x in pending_dcs)}")

    def _after_fail() -> None:
        # DC3 на WARP живёт на IPv4 — восстанавливаем семейство из playbook
        restore_v6 = bool(_playbook(orig_dc, proxy=bool(proxy), warp=warp)[0][0])
        _restore_dc(session_path, orig_dc, orig_key, restore_v6)

    for dc in pending_dcs:
        book = _playbook(dc, proxy=bool(proxy), warp=warp)
        tries = 4 if dc == 3 else 2
        for use_v6, conn, to in book:
            mode_timeout = max(int(timeout or 15), int(to))
            label = f"{'IPv6' if use_v6 else 'IPv4'} {_conn_name(conn)} DC{dc}"
            for attempt in range(1, tries + 1):
                client = _client(
                    session_path,
                    api_id,
                    api_hash,
                    proxy_raw=proxy,
                    connection_retries=1,
                    retry_delay=1,
                    timeout=mode_timeout,
                    use_ipv6=use_v6,
                    connection=conn,
                    dc_id=dc,
                    auth_key=auth_key,
                    persist=False,
                )
                tag = label if tries == 1 else f"{label} #{attempt}"
                try:
                    await asyncio.wait_for(client.connect(), timeout=mode_timeout + 6)
                    await asyncio.sleep(0.3)
                    sender = getattr(client, "_sender", None)
                    disc = getattr(sender, "_disconnected", None) if sender else None
                    if disc is not None and disc.done() and not disc.cancelled():
                        err = disc.exception()
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        last = err or ConnectionError("disconnected")
                        if _is_auth_miss(err):
                            if dc == home_dc:
                                auth_miss_home = True
                            log(f"{tag}: ключ не принят на DC{dc}")
                            _after_fail()
                            break
                        log(f"{tag}: {_fmt_exc(last)}")
                        _after_fail()
                        await asyncio.sleep(0.8)
                        continue
                    if client.is_connected():
                        try:
                            # Пишем только домашний DC + рабочий IP-режим — не чужие DC
                            key = client.session.auth_key or orig_key
                            _apply_dc(
                                client.session, home_dc, use_v6, key, persist=True
                            )
                        except Exception:
                            pass
                        log(f"связь ок · {tag}")
                        return client
                except SessionDeadError:
                    _after_fail()
                    raise
                except BaseException as exc:
                    last = exc
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    if _is_auth_miss(exc):
                        if dc == home_dc:
                            auth_miss_home = True
                        log(f"{tag}: ключ не принят на DC{dc}")
                        _after_fail()
                        break
                    log(f"{tag}: {_fmt_exc(exc)}")
                    _after_fail()
                    await asyncio.sleep(0.8)

    if auth_miss_kills and auth_miss_home and auth_key is not None:
        _after_fail()
        raise SessionDeadError(
            "Сессия Telegram слетела — нажми «ВОЙТИ В TELEGRAM»"
        ) from last
    if warp and not proxy and last:
        raise ConnectionError(_WARP_SOCKS_HINT) from last
    if last:
        raise last
    raise ConnectionError("Не удалось подключиться к Telegram")


_FRESH_DCS = (5, 2, 1, 4, 3)


async def connect_fresh_client(
    session_path,
    api_id: int,
    api_hash: str,
    *,
    proxy_raw: str | None = None,
    timeout: int = 14,
    on_log: LogFn = None,
) -> TelegramClient:
    """Новый ключ. WARP: DC5/DC2 IPv6 Obfuscated, DC3 — IPv4 TcpFull."""

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    proxy = resolve_telegram_proxy(proxy_raw)
    warp = warp_tunnel_on()
    last: BaseException | None = None
    for dc in _FRESH_DCS:
        for use_v6, conn, to in _playbook(dc, proxy=bool(proxy), warp=warp):
            host = (DC_IPV6 if use_v6 else DC_IPV4)[dc]
            # IPv4 probe на WARP врёт (таймаут), TcpFull при этом живой — не пропускаем.
            if use_v6 and not await _tcp_ok(host, 443, 2.5):
                log(f"DC{dc}: IPv6 TCP нет")
                continue
            tries = 3 if dc == 3 else 1
            for attempt in range(1, tries + 1):
                _unlink_session(session_path)
                mode_timeout = max(int(timeout or 14), int(to))
                label = f"{'IPv6' if use_v6 else 'IPv4'} {_conn_name(conn)} DC{dc}"
                if tries > 1:
                    label = f"{label} #{attempt}"
                client = _client(
                    session_path,
                    api_id,
                    api_hash,
                    proxy_raw=proxy,
                    connection_retries=1,
                    retry_delay=1,
                    timeout=mode_timeout,
                    use_ipv6=use_v6,
                    connection=conn,
                    dc_id=dc,
                    auth_key=None,
                    persist=False,
                )
                try:
                    await asyncio.wait_for(client.connect(), timeout=mode_timeout + 4)
                    if client.is_connected():
                        try:
                            _apply_dc(
                                client.session,
                                dc,
                                use_v6,
                                client.session.auth_key,
                                persist=True,
                            )
                        except Exception:
                            pass
                        log(f"связь ок · новый ключ · {label}")
                        return client
                except BaseException as exc:
                    last = exc
                    log(f"{label}: {_fmt_exc(exc)}")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    _unlink_session(session_path)
                    if attempt < tries:
                        await asyncio.sleep(0.8)
    if last:
        raise last
    raise ConnectionError("Не достучались до Telegram — проверь WARP и нажми вход ещё раз")


async def connect_saved_dc_client(
    session_path,
    api_id: int,
    api_hash: str,
    *,
    proxy_raw: str | None = None,
    timeout: int = 18,
    on_log: LogFn = None,
) -> TelegramClient:
    """Только DC из файла сессии — для ввода кода. Чужие DC не пишем."""
    return await connect_telegram_client(
        session_path,
        api_id,
        api_hash,
        proxy_raw=proxy_raw,
        timeout=timeout,
        on_log=on_log,
        roam_dcs=False,
        auth_miss_kills=False,
    )
