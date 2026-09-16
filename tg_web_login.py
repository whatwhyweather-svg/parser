"""Вход через web.telegram.org: пользователь логинится в Chrome, софт забирает сессию."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import tg_ssl  # noqa: F401  — до telethon

from playwright_env import fix_playwright_browsers_path
from tg_user_auth import (
    _env_set,
    _tg_err,
    force_reset_session,
    mark_user_ready,
    session_file,
)

LOGIN_URL = "https://web.telegram.org/k/"
WAIT_SEC = 600  # 10 мин на номер, код, 2FA

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

_DC_KEY = re.compile(r"^dc(\d+)_auth_key$", re.I)
_HEX512 = re.compile(r"^[0-9a-fA-F]{512}$")

# Web K: localStorage. Web A: IndexedDB. Не тащим переписку — только session/auth.
# dcN_auth_key появляется ещё ДО входа — по нему окно закрывать нельзя.
_EXTRACT_JS = r"""() => {
  const visible = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || Number(s.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4;
  };
  const ls = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    ls[k] = localStorage.getItem(k);
  }
  const ss = {};
  for (let i = 0; i < sessionStorage.length; i++) {
    const k = sessionStorage.key(i);
    ss[k] = sessionStorage.getItem(k);
  }
  const authRoot = document.querySelector(
    "#auth-pages, .auth-pages, .login-form, form.auth-form, .auth-image"
  );
  const phoneLike = document.querySelector(
    'input[type="tel"], input[autocomplete="tel"], input[name="phone"], input[inputmode="tel"]'
  );
  const onAuthScreen = visible(authRoot) || visible(phoneLike);

  const parseId = (raw) => {
    if (!raw || raw === "null" || raw === "false") return 0;
    try {
      const o = typeof raw === "string" ? JSON.parse(raw) : raw;
      const id = Number(o && (o.id || o.userId || o.user_id));
      return id > 0 ? id : 0;
    } catch (e) {
      return 0;
    }
  };
  const parseAcc = (raw) => {
    if (!raw) return 0;
    try {
      const o = typeof raw === "string" ? JSON.parse(raw) : raw;
      const id = Number(o && (o.userId || o.user_id || (o.user_auth && o.user_auth.id)));
      return id > 0 ? id : 0;
    } catch (e) {
      return 0;
    }
  };
  let userId = parseId(ls.user_auth || ls.userAuth || "");
  if (!userId) {
    for (const k of ["account1", "account2", "account3", "account4"]) {
      userId = parseAcc(ls[k]);
      if (userId) break;
    }
  }
  if (!userId) {
    for (const v of Object.values(ls)) {
      if (typeof v === "string" && v.includes('"id"') && /dcID|dcId|user/i.test(v)) {
        userId = parseId(v);
        if (userId) break;
      }
    }
  }
  const loggedIn = userId > 0 && !onAuthScreen;
  return { ls, ss, loggedIn, userId, onAuthScreen, url: location.href };
}"""

_IDB_JS = r"""async () => {
  function jsonable(v, depth) {
    if (depth > 6) return null;
    if (v == null) return v;
    const t = typeof v;
    if (t === "string" || t === "number" || t === "boolean") return v;
    if (v instanceof ArrayBuffer) return Array.from(new Uint8Array(v));
    if (ArrayBuffer.isView(v)) return Array.from(new Uint8Array(v.buffer, v.byteOffset, v.byteLength));
    if (Array.isArray(v)) return v.map((x) => jsonable(x, depth + 1));
    if (t === "object") {
      const o = {};
      for (const k of Object.keys(v)) {
        try { o[k] = jsonable(v[k], depth + 1); } catch (e) {}
      }
      return o;
    }
    return String(v);
  }
  const skip = /message|dialog|chat|media|document|photo|sticker|cache|blob|file/i;
  const keep = /auth|session|account|user|dc|key|gram|global/i;
  const out = [];
  let dbs = [];
  try { dbs = (await indexedDB.databases()) || []; } catch (e) { return out; }
  for (const info of dbs) {
    if (!info || !info.name) continue;
    try {
      const dump = await new Promise((resolve) => {
        const req = indexedDB.open(info.name);
        req.onerror = () => resolve(null);
        req.onsuccess = () => {
          const db = req.result;
          const names = [...db.objectStoreNames].filter((n) => keep.test(n) && !skip.test(n));
          if (!names.length) { db.close(); resolve({ name: info.name, stores: {} }); return; }
          const tx = db.transaction(names, "readonly");
          const stores = {};
          let left = names.length;
          const done = () => { if (--left <= 0) { db.close(); resolve({ name: info.name, stores }); } };
          for (const n of names) {
            const r = tx.objectStore(n).getAll();
            r.onsuccess = () => {
              const rows = r.result || [];
              stores[n] = rows.length > 40 ? rows.slice(0, 40) : rows;
              done();
            };
            r.onerror = () => { stores[n] = []; done(); };
          }
        };
      });
      if (dump) out.push({ name: dump.name, stores: jsonable(dump.stores, 0) });
    } catch (e) {}
  }
  return out;
}"""


def _log(on_log: Callable[[str], None] | None, msg: str) -> None:
    if on_log:
        on_log(msg)


def _jsonish(value: Any, depth: int = 0) -> Any:
    """tweb кладёт в localStorage JSON.stringify — иногда дважды."""
    if depth > 4 or value is None:
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return value
        if text[0] in '{["' or text in {"true", "false", "null"}:
            try:
                return _jsonish(json.loads(text), depth + 1)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, dict):
        return {k: _jsonish(v, depth + 1) for k, v in value.items()}
    return value


def _to_auth_bytes(value: Any) -> bytes | None:
    value = _jsonish(value)
    if value is None or value is False:
        return None
    if isinstance(value, (bytes, bytearray)) and len(value) == 256:
        return bytes(value)
    if isinstance(value, list) and len(value) == 256:
        if all(isinstance(x, int) and 0 <= x <= 255 for x in value):
            return bytes(value)
    if isinstance(value, dict):
        for k in ("key", "authKey", "auth_key", "data", "bytes"):
            got = _to_auth_bytes(value.get(k))
            if got:
                return got
        return None
    if not isinstance(value, str):
        return None
    text = value.strip().strip('"').strip("'")
    if text in {"", "false", "null", "undefined", "[]"}:
        return None
    if text.startswith("[") or text.startswith("{"):
        try:
            return _to_auth_bytes(json.loads(text))
        except json.JSONDecodeError:
            pass
    hex_s = re.sub(r"[^0-9a-fA-F]", "", text)
    if _HEX512.match(hex_s):
        return bytes.fromhex(hex_s)
    return None


def _is_ready(blob: dict[str, Any] | None) -> bool:
    if not blob:
        return False
    if blob.get("onAuthScreen"):
        return False
    try:
        return int(blob.get("userId") or 0) > 0
    except (TypeError, ValueError):
        return False


def _push_dc(dcs: list[int], raw: Any) -> None:
    try:
        n = int(_jsonish(raw))
    except (TypeError, ValueError):
        return
    if 1 <= n <= 5:
        dcs.append(n)


def _ingest(obj: Any, by_dc: dict[int, bytes], dcs: list[int], depth: int = 0) -> None:
    if depth > 8 or obj is None:
        return
    obj = _jsonish(obj)
    if isinstance(obj, dict):
        for k in ("dcId", "dcID", "dc", "user_dc", "baseDcId"):
            if obj.get(k) is not None:
                _push_dc(dcs, obj.get(k))
        for k, v in obj.items():
            m = _DC_KEY.match(str(k))
            if m:
                b = _to_auth_bytes(v)
                if b:
                    by_dc[int(m.group(1))] = b
            elif isinstance(v, (dict, list, str)):
                _ingest(v, by_dc, dcs, depth + 1)
        return
    if isinstance(obj, list):
        for item in obj[:20]:
            _ingest(item, by_dc, dcs, depth + 1)


def _parse_auth(blob: dict[str, Any]) -> tuple[int, bytes] | None:
    ls = blob.get("ls") or {}
    ss = blob.get("ss") or {}
    idb = blob.get("idb") or []
    dcs: list[int] = []
    by_dc: dict[int, bytes] = {}

    current = _jsonish(ls.get("current_account") or ss.get("current_account") or 1)
    try:
        current_n = int(current or 1)
    except (TypeError, ValueError):
        current_n = 1
    acc_first = f"account{current_n}"
    for key in (acc_first, "account1", "account2", "account3", "account4"):
        if key in ls:
            _ingest(ls.get(key), by_dc, dcs)

    _ingest(ls.get("user_auth") or ls.get("userAuth"), by_dc, dcs)
    _push_dc(dcs, ls.get("dc") or ls.get("dcId") or ls.get("dc_id") or ss.get("dc"))

    for store in (ls, ss):
        for k, v in store.items():
            m = _DC_KEY.match(str(k))
            if not m:
                continue
            b = _to_auth_bytes(v)
            if b:
                by_dc[int(m.group(1))] = b

    _ingest(idb, by_dc, dcs)

    dc = next((x for x in dcs if 1 <= x <= 5), 0)
    if dc and dc in by_dc:
        return dc, by_dc[dc]
    if by_dc:
        pick = dc if dc in by_dc else max(by_dc)
        return pick, by_dc[pick]
    return None


def _write_session(path: Path, dc_id: int, auth_key: bytes, *, ipv6: bool) -> None:
    from telethon.crypto import AuthKey
    from telethon.sessions import SQLiteSession

    for leftover in path.parent.glob(path.name + "*"):
        try:
            leftover.unlink()
        except OSError:
            pass

    ip_map = DC_IPV6 if ipv6 else DC_IPV4
    ip = ip_map.get(dc_id) or ip_map[2]
    sess = SQLiteSession(str(path))
    sess.set_dc(int(dc_id), ip, 443)
    sess.auth_key = AuthKey(data=auth_key)
    sess.save()
    sess.close()


async def _verify_session(
    path: Path,
    dc_id: int,
    auth_key: bytes,
    api_id: int,
    api_hash: str,
    proxy_raw: str | None,
) -> str:
    """Пустая строка = ок. Иначе ошибка. Не даём Telethon сбросить DC на 2."""
    from tg_mtproto import SessionDeadError, connect_telegram_client, warp_tunnel_on

    _write_session(path, dc_id, auth_key, ipv6=warp_tunnel_on())
    client = None
    try:
        client = await connect_telegram_client(
            path,
            api_id,
            api_hash,
            proxy_raw=proxy_raw,
            timeout=20,
        )
        if not await client.is_user_authorized():
            return "В браузере вход не дошёл до списка чатов"
        me = await client.get_me()
        name = getattr(me, "username", None) or getattr(me, "first_name", None) or me.id
        return f"ok:{name}"
    except SessionDeadError:
        return "Ключ из браузера Telegram не принял"
    except BaseException as exc:
        return _tg_err(exc)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass


def _launch_browser(pw):
    from playwright_env import CHROME_ARGS

    try:
        return pw.chromium.launch(headless=False, channel="chrome", args=list(CHROME_ARGS))
    except Exception:
        return pw.chromium.launch(headless=False, args=list(CHROME_ARGS))


def run_telegram_web_login(
    *,
    on_log: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Открыть вход Telegram в Chrome, поймать сессию. Пустая строка = ок."""
    from playwright.sync_api import sync_playwright

    from config import load_settings, resolve_telegram_api

    _env_set("TELEGRAM_USER_ENABLED", "true")
    force_reset_session()
    settings = load_settings()
    path = Path(getattr(settings, "telegram_user_session", None) or session_file() or "tg_user.session")
    aid, ahash = resolve_telegram_api()

    fix_playwright_browsers_path()
    _log(on_log, "Открываю вход Telegram в Chrome — войди там как обычно (номер и код)")

    pw = sync_playwright().start()
    browser = None
    parsed: tuple[int, bytes] | None = None
    try:
        browser = _launch_browser(pw)
        context = browser.new_context(
            viewport={"width": 1280, "height": 860},
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            user_agent=getattr(settings, "user_agent", None) or None,
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        page.set_default_timeout(600_000)
        page.set_default_navigation_timeout(90_000)
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90_000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        _log(on_log, "Окно не закрою, пока не войдёшь и не появится список чатов")

        deadline = time.time() + WAIT_SEC
        last_ping = 0.0
        ready_since: float | None = None
        while time.time() < deadline:
            if should_stop and should_stop():
                return "Отмена"
            if page.is_closed():
                return "Окно браузера закрыли до входа"

            try:
                blob = page.evaluate(_EXTRACT_JS)
            except Exception:
                ready_since = None
                page.wait_for_timeout(1500)
                continue

            if _is_ready(blob):
                if ready_since is None:
                    ready_since = time.time()
                    _log(on_log, "Вижу вход, ещё секунда — забираю сессию…")
                # Дать Web K дописать user_auth / ключ, не закрывать сразу
                if time.time() - ready_since < 8:
                    page.wait_for_timeout(800)
                    continue
                try:
                    blob = page.evaluate(_EXTRACT_JS)
                    blob["idb"] = page.evaluate(_IDB_JS)
                except Exception:
                    blob["idb"] = blob.get("idb") or []
                if not _is_ready(blob):
                    ready_since = None
                    page.wait_for_timeout(1500)
                    continue
                parsed = _parse_auth(blob)
                if parsed:
                    _log(on_log, f"Сессия из браузера поймана (DC{parsed[0]}), проверяю…")
                    break
                ready_since = None
            else:
                ready_since = None

            now = time.time()
            if now - last_ping > 25:
                _log(on_log, "Жду вход в открывшемся Chrome (номер → код → чаты)…")
                last_ping = now
            page.wait_for_timeout(1500)
        else:
            return "Время вышло. Войди в Chrome до списка чатов и нажми «ВОЙТИ В TELEGRAM» ещё раз"
    except Exception as exc:
        return _tg_err(exc)
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    if not parsed:
        return "Не удалось забрать сессию. Дождись списка чатов в Chrome и повтори вход."

    dc_id, auth_key = parsed
    try:
        result = asyncio.run(
            _verify_session(
                path,
                dc_id,
                auth_key,
                int(aid),
                ahash,
                getattr(settings, "telegram_proxy", None),
            )
        )
    except Exception as exc:
        return _tg_err(exc)

    if result.startswith("ok:"):
        mark_user_ready()
        name = result[3:]
        _log(on_log, f"Telegram: сессия сохранена · @{name}")
        return ""
    return result


if __name__ == "__main__":
    err = run_telegram_web_login(on_log=print)
    raise SystemExit(0 if not err else print(err) or 1)
