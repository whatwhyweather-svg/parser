"""Вход личного аккаунта Telegram (Telethon) — бот в группу не нужен."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import tg_ssl  # noqa: F401  — до telethon

from telethon.errors import SessionPasswordNeededError

from config import BASE_DIR, load_settings, resolve_telegram_api
from tg_mtproto import connect_telegram_client

READY_MARK = BASE_DIR / "tg_user.ok"
LOGIN_STATE = BASE_DIR / "tg_login_state.json"
LOGIN_RESULT = BASE_DIR / "tg_login_result.json"
PY = BASE_DIR / ".venv" / "Scripts" / "python.exe"


def _env_set(key: str, value: str) -> None:
    path = BASE_DIR / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"(?m)^{key}=", text):
        text = re.sub(rf"(?m)^{key}=.*$", line, text)
    else:
        text = text.rstrip() + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")


def normalize_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return ""
    return "+" + digits


def phone_format_hint(phone: str) -> str | None:
    """Пусто = длина ок. Иначе текст, что не так."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if not digits:
        return "Введи номер с кодом страны, как в Telegram → Настройки"
    # Непал: +977 + 10 цифр мобильного (98… / 97…)
    if digits.startswith("977"):
        nsn = digits[3:]
        if len(nsn) != 10:
            return (
                f"Непал: +977 и 10 цифр. Сейчас после 977 стоит {len(nsn)}. "
                "Сверь с номером в приложении Telegram."
            )
        return None
    if len(digits) < 10:
        return "Слишком короткий номер. Нужен + и код страны, как в Telegram."
    return None


def save_phone_to_env(phone: str) -> None:
    phone = normalize_phone(phone)
    if phone:
        _env_set("TELEGRAM_USER_PHONE", phone)
    _env_set("TELEGRAM_USER_ENABLED", "true")


def mark_user_ready() -> None:
    READY_MARK.write_text("1", encoding="utf-8")


def clear_user_ready() -> None:
    try:
        READY_MARK.unlink(missing_ok=True)
    except OSError:
        pass


def force_reset_session() -> None:
    """Снести мёртвый auth key, чтобы следующий вход создал новый."""
    clear_user_ready()
    path = session_file()
    if path is None:
        path = BASE_DIR / "tg_user.session"
    for _ in range(6):
        leftover = False
        for p in path.parent.glob(path.name + "*"):
            try:
                p.unlink()
            except OSError:
                leftover = True
        if not leftover:
            return
        time.sleep(0.4)


def _code_hint(sent) -> str:
    name = type(getattr(sent, "type", None)).__name__
    if "App" in name:
        return (
            "Код в чате Telegram на телефоне: «Telegram» (777000). "
            "Если пусто — SMS."
        )
    if "Sms" in name:
        return "Код отправили SMS. Если пусто — чат Telegram (777000)."
    if "Call" in name or "Missed" in name:
        return "Telegram звонит на номер — код в звонке."
    if "Email" in name:
        return "Код отправили на email аккаунта Telegram."
    return (
        "Код отправили. Смотри приложение Telegram (чат 777000), не SMS."
    )


def last_code_hint() -> str:
    if LOGIN_STATE.exists():
        try:
            data = json.loads(LOGIN_STATE.read_text(encoding="utf-8"))
            hint = str(data.get("hint") or "").strip()
            if hint:
                return hint
        except (OSError, json.JSONDecodeError):
            pass
    return (
        "Код в чате Telegram (777000) на телефоне. "
        "Если там пусто — смотри SMS."
    )


def _tg_err(exc: BaseException) -> str:
    text = str(exc)
    low = text.casefold()
    name = type(exc).__name__
    seconds = getattr(exc, "seconds", None)
    if name == "FloodWaitError" and seconds:
        return f"Telegram просит подождать {int(seconds)}с, потом вход ещё раз."
    if name in {"PhoneNumberFloodError"}:
        return "Слишком много запросов кода. Подожди час и попробуй снова."
    if name in {"PhoneNumberInvalidError"}:
        hint = phone_format_hint(text)
        return hint or (
            "Telegram не принял номер. Пиши как в приложении: "
            "+код страны и цифры, без пробелов."
        )
    if name in {"PhoneNumberBannedError"}:
        return "Этот номер заблокирован в Telegram."
    if "database is locked" in low:
        return "Сессия занята. Закрой лишние окна и нажми вход ещё раз."
    if "failed" in low or "timeout" in low or "connection" in low:
        return "Не достучались до Telegram. Подожди 5с и нажми вход ещё раз."
    return text[:240]


def session_file() -> Path | None:
    try:
        settings = load_settings()
    except Exception:
        return BASE_DIR / "tg_user.session"
    return getattr(settings, "telegram_user_session", None)


def session_file_ready() -> bool:
    path = session_file()
    try:
        return bool(path and path.exists() and path.stat().st_size > 1000)
    except OSError:
        return False


def session_authorized() -> bool:
    """
    Готовый вход = есть файл сессии.
    Метка tg_user.ok — кэш; отсутствие метки больше НЕ требует повторного входа
    и не провоцирует снос файла через «ВОЙТИ».
    """
    if not session_file_ready():
        return False
    if not READY_MARK.exists():
        try:
            mark_user_ready()
        except OSError:
            pass
    return True


def _wipe_broken_session(path: Path) -> None:
    """Сносить файл только если нет ни метки, ни нормального размера сессии."""
    if READY_MARK.exists() or session_file_ready():
        return
    for p in path.parent.glob(path.name + "*"):
        try:
            p.unlink()
        except OSError:
            pass


def _force_utf8() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _write_result(ok: bool, msg: str = "") -> None:
    LOGIN_RESULT.write_text(
        json.dumps({"ok": bool(ok), "msg": msg or ""}, ensure_ascii=True),
        encoding="utf-8",
    )


def _read_result() -> tuple[bool, str] | None:
    if not LOGIN_RESULT.exists():
        return None
    try:
        data = json.loads(LOGIN_RESULT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return bool(data.get("ok")), str(data.get("msg") or "")


def _readable_err(text: str) -> str:
    raw = (text or "").strip()
    if raw.count("\ufffd") >= 2:
        return "Не достучались до Telegram. Нажми вход ещё раз."
    return raw[:240] if raw else "ошибка входа"


async def _safe_disconnect(client) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), 4)
    except Exception:
        pass


def _pending_code(phone: str) -> bool:
    """Код только что запросили — повторно слать не надо (макс. 60с)."""
    phone = normalize_phone(phone)
    if not phone or not LOGIN_STATE.exists():
        return False
    try:
        data = json.loads(LOGIN_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if normalize_phone(str(data.get("phone") or "")) != phone:
        return False
    if not str(data.get("phone_code_hash") or "").strip():
        return False
    try:
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    if not ts or (time.time() - ts) > 60:
        return False
    return session_file_ready()


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_worker(*args: str, timeout: int = 180) -> str:
    """Отдельный процесс = как успешный CLI-тест, без конфликтов с GUI/ботом."""
    try:
        LOGIN_RESULT.unlink(missing_ok=True)
    except OSError:
        pass
    exe = str(PY if PY.exists() else sys.executable)
    proc = subprocess.run(
        [exe, str(BASE_DIR / "tg_user_auth.py"), *args],
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_worker_env(),
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    parsed = _read_result()
    if parsed is not None:
        ok, msg = parsed
        if ok:
            return ""
        return _readable_err(msg or "ошибка входа")
    if "OK:CODE_SENT" in out or "OK:SIGNED" in out:
        return ""
    if proc.returncode == 0:
        return ""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("ERR:"):
            return _readable_err(line[4:].strip())
    return _readable_err(out[-240:] if out else f"ошибка входа (код {proc.returncode})")


def probe_session_alive() -> bool:
    """Быстрая проверка: файл сессии ещё принимает Telegram."""
    if not session_file_ready():
        return False
    try:
        from instance_lock import (
            acquire_telethon_session_lock,
            release_telethon_session_lock,
        )
    except Exception:
        acquire_telethon_session_lock = None  # type: ignore
        release_telethon_session_lock = None  # type: ignore
    if acquire_telethon_session_lock is not None:
        if not acquire_telethon_session_lock(wait_ms=400):
            # Слушатель уже держит сессию — значит вход живой
            return True
    try:
        out = _run_worker("ping", timeout=45)
        return out == ""
    except Exception:
        return False
    finally:
        if release_telethon_session_lock is not None:
            try:
                release_telethon_session_lock()
            except Exception:
                pass


async def _cmd_ping() -> None:
    """Проверить, что сохранённая сессия ещё авторизована. Без сноса файла."""
    from tg_mtproto import SessionDeadError, connect_telegram_client

    settings = load_settings()
    path = Path(settings.telegram_user_session)
    if not path.exists() or path.stat().st_size < 1000:
        print("ERR:нет сессии", flush=True)
        _write_result(False, "нет сессии")
        raise SystemExit(2)
    aid, ahash = resolve_telegram_api()
    try:
        client = await connect_telegram_client(
            path,
            int(aid),
            ahash,
            proxy_raw=getattr(settings, "telegram_proxy", None),
            timeout=18,
            roam_dcs=False,
            auth_miss_kills=True,
        )
    except SessionDeadError as exc:
        print(f"ERR:{exc}", flush=True)
        _write_result(False, str(exc))
        raise SystemExit(2)
    try:
        if not await client.is_user_authorized():
            print("ERR:не авторизован", flush=True)
            _write_result(False, "не авторизован")
            raise SystemExit(2)
        await client.get_me()
        mark_user_ready()
        print("OK:PING", flush=True)
        _write_result(True, "PING")
    finally:
        await _safe_disconnect(client)


def send_login_code(phone: str) -> str:
    """Пустая строка = код ушёл (или уже вошли). Иначе ошибка."""
    phone = normalize_phone(phone)
    hint = phone_format_hint(phone)
    if hint:
        return hint
    if not phone.startswith("+") or len(phone) < 11:
        return "Введи номер как в Telegram: +код страны и цифры"
    save_phone_to_env(phone)
    if _pending_code(phone):
        return ""
    # Уже вошли — не сносим сессию «на всякий случай»
    if session_file_ready() and probe_session_alive():
        mark_user_ready()
        return ""
    force_reset_session()
    try:
        return _run_worker("send", phone, timeout=90)
    except subprocess.TimeoutExpired:
        return "Таймаут связи с Telegram — нажми вход ещё раз"
    except Exception as exc:
        return _tg_err(exc)


def finish_login(code: str, password: str = "") -> str:
    """Пустая строка = вошли."""
    code = (code or "").strip()
    password = (password or "").strip()
    if not code and not password:
        return "Введи код из Telegram"
    try:
        args = ["sign", code]
        if password:
            args.extend(["--password", password])
        result = _run_worker(*args)
        if result == "2FA":
            return "2FA"
        return result
    except subprocess.TimeoutExpired:
        return "Таймаут при входе — попробуй ещё раз"
    except Exception as exc:
        return _tg_err(exc)


async def _cmd_send(phone: str) -> None:
    from telethon.errors import (
        FloodWaitError,
        PhoneNumberBannedError,
        PhoneNumberFloodError,
        PhoneNumberInvalidError,
    )
    from tg_mtproto import connect_fresh_client

    phone = normalize_phone(phone)
    settings = load_settings()
    path = Path(settings.telegram_user_session)
    _wipe_broken_session(path)
    aid, ahash = resolve_telegram_api()
    client = await connect_fresh_client(
        path,
        int(aid),
        ahash,
        proxy_raw=getattr(settings, "telegram_proxy", None),
        timeout=16,
    )
    try:
        sent = await client.send_code_request(phone)
        kind = type(getattr(sent, "type", None)).__name__
        if "App" in kind:
            try:
                sent = await client.send_code_request(phone)
            except Exception:
                pass
        hash_ = getattr(sent, "phone_code_hash", None) or ""
        LOGIN_STATE.write_text(
            json.dumps(
                {
                    "phone": phone,
                    "phone_code_hash": hash_,
                    "hint": _code_hint(sent),
                    "ts": time.time(),
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )
        print("OK:CODE_SENT", flush=True)
        _write_result(True, "CODE_SENT")
    except (
        FloodWaitError,
        PhoneNumberBannedError,
        PhoneNumberFloodError,
        PhoneNumberInvalidError,
    ) as exc:
        raise RuntimeError(_tg_err(exc)) from exc
    finally:
        await _safe_disconnect(client)


async def _cmd_sign(code: str, password: str = "") -> None:
    from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError
    from tg_mtproto import connect_saved_dc_client

    if not LOGIN_STATE.exists():
        print("ERR:Сначала запроси код", flush=True)
        _write_result(False, "Сначала запроси код")
        raise SystemExit(2)
    state = json.loads(LOGIN_STATE.read_text(encoding="utf-8"))
    phone = state.get("phone") or ""
    phone_hash = state.get("phone_code_hash") or ""
    if not phone:
        print("ERR:Сначала запроси код", flush=True)
        _write_result(False, "Сначала запроси код")
        raise SystemExit(2)

    settings = load_settings()
    path = Path(settings.telegram_user_session)
    if not path.exists() or path.stat().st_size < 1000:
        print("ERR:Сессия сбросилась — нажми вход ещё раз, придёт новый код", flush=True)
        _write_result(False, "Сессия сбросилась — нажми вход ещё раз, придёт новый код")
        try:
            LOGIN_STATE.unlink(missing_ok=True)
        except OSError:
            pass
        raise SystemExit(2)
    aid, ahash = resolve_telegram_api()
    client = await connect_saved_dc_client(
        path,
        int(aid),
        ahash,
        proxy_raw=getattr(settings, "telegram_proxy", None),
        timeout=22,
    )
    try:
        if password and not code:
            await client.sign_in(password=password)
        else:
            try:
                if phone_hash:
                    await client.sign_in(phone, code, phone_code_hash=phone_hash)
                else:
                    await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                if not password:
                    print("ERR:2FA", flush=True)
                    _write_result(False, "2FA")
                    raise SystemExit(3)
                await client.sign_in(password=password)
            except PhoneCodeExpiredError as exc:
                try:
                    LOGIN_STATE.unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(
                    "Код устарел — нажми вход ещё раз, придёт новый"
                ) from exc
            except PhoneCodeInvalidError as exc:
                raise RuntimeError("Неверный код — проверь чат Telegram (777000)") from exc
        mark_user_ready()
        try:
            LOGIN_STATE.unlink(missing_ok=True)
        except OSError:
            pass
        print("OK:SIGNED", flush=True)
        _write_result(True, "SIGNED")
    finally:
        await _safe_disconnect(client)


def run_qr_login(
    *,
    on_qr_url: Callable[[str], None] | None = None,
    ask_2fa: Callable[[], str] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Вход по QR. Пустая строка = ок. Код/SMS не нужны."""
    save_phone_to_env("")
    _env_set("TELEGRAM_USER_ENABLED", "true")
    force_reset_session()
    try:
        return asyncio.run(_async_qr_login(on_qr_url, ask_2fa, should_stop))
    except Exception as exc:
        return _tg_err(exc)


async def _async_qr_login(
    on_qr_url: Callable[[str], None] | None,
    ask_2fa: Callable[[], str] | None,
    should_stop: Callable[[], bool] | None,
) -> str:
    from tg_mtproto import SessionDeadError, connect_fresh_client, connect_telegram_client

    settings = load_settings()
    path = Path(settings.telegram_user_session)
    aid, ahash = resolve_telegram_api()

    async def _connect_fresh():
        return await connect_fresh_client(
            path,
            int(aid),
            ahash,
            proxy_raw=getattr(settings, "telegram_proxy", None),
            timeout=16,
        )

    try:
        client = await _connect_fresh()
    except SessionDeadError:
        force_reset_session()
        client = await _connect_fresh()
    try:
        if await client.is_user_authorized():
            mark_user_ready()
            return ""
        deadline = time.time() + 180
        while time.time() < deadline:
            if should_stop and should_stop():
                return "Отмена"
            qr = await client.qr_login()
            if on_qr_url:
                on_qr_url(qr.url)
            left = (
                qr.expires - datetime.now(tz=timezone.utc)
            ).total_seconds() - 1
            wait_until = time.time() + max(5.0, min(left, deadline - time.time()))
            logged = False
            while time.time() < wait_until:
                if should_stop and should_stop():
                    return "Отмена"
                try:
                    await qr.wait(timeout=2)
                    logged = True
                    break
                except asyncio.TimeoutError:
                    continue
                except SessionPasswordNeededError:
                    pwd = ask_2fa() if ask_2fa else ""
                    if not (pwd or "").strip():
                        return "Нужен облачный пароль 2FA"
                    await client.sign_in(password=str(pwd).strip())
                    logged = True
                    break
            if logged:
                break
        else:
            return "QR истёк — нажми вход ещё раз"
        if not await client.is_user_authorized():
            return "Не вышло войти по QR"
        await client.get_me()
        try:
            await client.disconnect()
        except Exception:
            pass
        # Как у рабочей сессии @svscared: тот же MTProto, что и START
        client = await connect_telegram_client(
            path,
            int(aid),
            ahash,
            proxy_raw=getattr(settings, "telegram_proxy", None),
            timeout=20,
        )
        if not await client.is_user_authorized():
            return "Вошли, но повторная связь не прошла — нажми вход ещё раз"
        mark_user_ready()
        return ""
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def _main(argv: list[str]) -> int:
    _force_utf8()
    if len(argv) < 2:
        print("ERR:usage", flush=True)
        _write_result(False, "usage")
        return 1
    cmd = argv[1]
    from instance_lock import (
        acquire_telethon_session_lock,
        release_telethon_session_lock,
    )

    # Вход/ping тоже берут тот же слот — иначе AuthKeyDuplicated
    if not acquire_telethon_session_lock(wait_ms=15000):
        print("ERR:сессия занята слушателем — сначала STOP в программе", flush=True)
        _write_result(False, "сессия занята — нажми STOP, потом ВОЙТИ")
        return 3
    try:
        if cmd == "ping":
            try:
                asyncio.run(_cmd_ping())
                return 0
            except SystemExit as e:
                return int(e.code or 1)
        if cmd == "send":
            asyncio.run(_cmd_send(argv[2] if len(argv) > 2 else ""))
            return 0
        if cmd == "sign":
            code = argv[2] if len(argv) > 2 else ""
            password = ""
            if "--password" in argv:
                i = argv.index("--password")
                password = argv[i + 1] if i + 1 < len(argv) else ""
            try:
                asyncio.run(_cmd_sign(code, password))
                return 0
            except SystemExit as e:
                return int(e.code or 1)
        print("ERR:unknown", flush=True)
        _write_result(False, "unknown")
        return 1
    except Exception as exc:
        msg = _tg_err(exc)
        print(f"ERR:{msg}", flush=True)
        _write_result(False, msg)
        return 1
    finally:
        release_telethon_session_lock()


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
