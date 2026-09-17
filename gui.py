"""Threads Posts ARTFrance — Threads #теги → Telegram."""

from __future__ import annotations

import math
import os
import random
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from config import load_settings
from database import PostDatabase
from hashtags import display_hashtag, normalize_hashtags
from session_store import session_exists
from tg_bot import TelegramControlBot
from tg_user_source import TelegramUserSource
from ui_settings import (
    UISettings,
    load_ui_settings,
    normalize_tg_ids,
    save_ui_settings,
)
from worker import ParserWorker, WorkerStats

# подхватить PARSER_AUTOSTART из .env (load_settings уже грузит dotenv)
_ = load_settings

APP_NAME = "Threads Posts ARTFrance"

C = {
    "bg": "#07070C",
    "panel": "#101018",
    "panel2": "#16161F",
    "line": "#2C2A3A",
    "ink": "#F4F1FA",
    "muted": "#9A93AD",
    "dim": "#5C5668",
    "ok": "#C4B5E8",
    "warn": "#B0B0B0",
    "danger": "#FFFFFF",
    "chip": "#1C1B27",
    "log_bg": "#0A0A12",
    "log_fg": "#D4CFE0",
    "accent": "#C4B5E8",
    "accent2": "#D8CCF4",
    "accent_dim": "#3A3350",
}

R = 14

STATUS = {
    "idle": ("IDLE", C["dim"]),
    "starting": ("STARTING", C["accent"]),
    "running": ("LIVE", C["accent"]),
    "scanning": ("SCAN", C["accent2"]),
    "sleeping": ("WATCH", C["muted"]),
    "stopping": ("STOP", C["muted"]),
    "error": ("ERROR", C["ink"]),
}


class SeasideApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.ui_cfg = load_ui_settings()
        self.title(APP_NAME)
        self.geometry(self.ui_cfg.geometry or "1280x800")
        self.minsize(1080, 720)
        self.configure(fg_color=C["bg"])
        self._anim_t = 0.0
        self._status_key = "idle"
        self._orbs: list[dict] = []

        try:
            self.settings = load_settings()
        except ValueError as exc:
            messagebox.showerror("Config", str(exc))
            self.settings = None

        self.worker: ParserWorker | None = None
        self.tg_bot: TelegramControlBot | None = None
        self.tg_user: TelegramUserSource | None = None
        self._busy_login = False
        self._db: PostDatabase | None = None
        if self.settings:
            self._db = PostDatabase(self.settings.database_path)

        if self.ui_cfg.hashtags:
            self._hashtags = list(self.ui_cfg.hashtags)
        elif self.settings:
            self._hashtags = list(self.settings.search_hashtags)
        else:
            self._hashtags = []

        self.interval_var = tk.StringVar(value=str(self.ui_cfg.interval_minutes or 2))
        self.parse_var = tk.StringVar(value=str(self.ui_cfg.parse_posts or 40))
        self.headless_var = tk.BooleanVar(value=bool(self.ui_cfg.headless))
        self._allowed_ids: list[int] = list(self.ui_cfg.allowed_tg_ids or [])
        if not self._allowed_ids and self.settings:
            self._allowed_ids = list(self.settings.allowed_tg_ids or [])

        self._build()
        self._render_chips()
        self._render_allowed()
        self._set_status("idle")
        self._boot_log()
        # Бот не стартуем сразу: getUpdates 409 в консоли выглядит как «вход сломался».
        self.after(8000, self._start_tg_bot_when_idle)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(40, self._tick_motion)
        self.after(2500, self._poll_access_users)
        self.after(2000, self._pulse_heartbeat)
        self._ensure_watchdog()
        # Сразу в работу: без START лиды не идут (PARSER_AUTOSTART=0 — только вручную)
        auto = os.environ.get("PARSER_AUTOSTART", "1").strip().lower()
        if auto not in {"0", "false", "no", "off"} and self._hashtags:
            self.after(1200, self._start)

    def _entry_kw(self, **extra):
        kw = dict(
            height=38,
            fg_color=C["panel2"],
            border_color=C["line"],
            border_width=1,
            text_color=C["ink"],
            placeholder_text_color=C["dim"],
            corner_radius=10,
        )
        kw.update(extra)
        return kw

    def _build(self) -> None:
        self.fog = tk.Canvas(self, bg=C["bg"], highlightthickness=0, bd=0)
        self.fog.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._seed_orbs()

        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.place(relx=0, rely=0, relwidth=1, relheight=1)

        # ── HEADER ──
        head = ctk.CTkFrame(shell, fg_color="transparent", height=108)
        head.pack(fill="x", padx=28, pady=(16, 8))
        head.pack_propagate(False)
        self._head_stage = ctk.CTkFrame(head, fg_color="transparent")
        self._head_stage.place(relx=0, rely=0, relwidth=1, relheight=1)

        titles = ctk.CTkFrame(self._head_stage, fg_color="transparent")
        titles.place(x=4, y=18)
        ctk.CTkLabel(
            titles,
            text="ARTFRANCE",
            font=ctk.CTkFont(family="Arial Black", size=30, weight="bold"),
            text_color=C["ink"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            titles,
            text="THREADS  →  TELEGRAM",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=C["accent"],
        ).pack(anchor="w", pady=(2, 0))
        ctk.CTkLabel(
            titles,
            text="лиды из тегов и из группы",
            font=ctk.CTkFont(size=12),
            text_color=C["muted"],
        ).pack(anchor="w", pady=(4, 0))

        self.status_pill = ctk.CTkLabel(
            self._head_stage,
            text="IDLE",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=C["accent"],
            fg_color=C["accent_dim"],
            corner_radius=20,
            width=96,
            height=32,
        )
        self.status_pill.place(relx=1.0, x=-8, y=28, anchor="ne")

        body = ctk.CTkFrame(shell, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(0, 8))
        body.grid_columnconfigure(0, weight=0, minsize=332)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(
            body,
            fg_color=C["panel"],
            corner_radius=R,
            border_width=1,
            border_color=C["line"],
            width=332,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16))

        self._h(left, "ДВИЖОК")
        self.start_btn = ctk.CTkButton(
            left,
            text="START",
            height=52,
            fg_color=C["accent"],
            hover_color=C["accent2"],
            text_color="#1A1228",
            font=ctk.CTkFont(family="Arial Black", size=18, weight="bold"),
            corner_radius=12,
            command=self._start,
        )
        self.start_btn.pack(fill="x", padx=16, pady=(0, 8))
        self.stop_btn = ctk.CTkButton(
            left,
            text="STOP",
            height=40,
            fg_color="transparent",
            hover_color=C["accent_dim"],
            border_width=1,
            border_color=C["accent"],
            text_color=C["accent"],
            font=ctk.CTkFont(size=14, weight="bold"),
            corner_radius=12,
            state="disabled",
            command=self._stop,
        )
        self.stop_btn.pack(fill="x", padx=16, pady=(0, 6))
        self.hint_label = ctk.CTkLabel(
            left,
            text="ВОЙТИ → START → ждём лиды",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=C["dim"],
        )
        self.hint_label.pack(anchor="w", padx=16, pady=(0, 14))

        self._h(left, "ТЕГИ")
        ctk.CTkLabel(
            left,
            text="В GUI или в боте: /add SEO · /del SEO",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
        ).pack(anchor="w", padx=16, pady=(0, 6))
        self.tag_entry = ctk.CTkEntry(
            left, placeholder_text="SEO, GEO (через запятую)", **self._entry_kw(height=40)
        )
        self.tag_entry.pack(fill="x", padx=16, pady=(0, 8))
        self.tag_entry.bind("<Return>", lambda _e: self._add_hashtag())
        ctk.CTkButton(
            left,
            text="ДОБАВИТЬ ТЕГ",
            height=36,
            fg_color=C["ink"],
            hover_color=C["accent2"],
            text_color="#1A1228",
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=10,
            command=self._add_hashtag,
        ).pack(fill="x", padx=16, pady=(0, 10))
        self.chips_frame = ctk.CTkFrame(
            left,
            fg_color=C["panel2"],
            corner_radius=10,
            border_width=1,
            border_color=C["line"],
        )
        self.chips_frame.pack(fill="x", padx=16, pady=(0, 16))

        self._h(left, "ВХОД")
        self.login_btn = ctk.CTkButton(
            left,
            text="ВОЙТИ В THREADS",
            height=44,
            fg_color="transparent",
            hover_color=C["accent_dim"],
            border_width=1,
            border_color=C["accent"],
            text_color=C["accent"],
            font=ctk.CTkFont(size=13, weight="bold"),
            corner_radius=12,
            command=self._login_threads,
        )
        self.login_btn.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="На телефоне: Настройки → Устройства → QR",
            font=ctk.CTkFont(size=11),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(
            left,
            text="Номер Telegram",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self.tg_phone_entry = ctk.CTkEntry(
            left, placeholder_text="+9779812345678", **self._entry_kw()
        )
        self.tg_phone_entry.pack(fill="x", padx=16, pady=(0, 6))
        phone0 = ""
        if self.settings:
            phone0 = (getattr(self.settings, "telegram_user_phone", "") or "").strip()
        if phone0:
            self.tg_phone_entry.insert(0, phone0)
        ctk.CTkLabel(
            left,
            text="Код: чат Telegram (777000). Если пусто — SMS",
            font=ctk.CTkFont(size=11),
            text_color=C["muted"],
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))
        self.tg_user_btn = ctk.CTkButton(
            left,
            text="ВОЙТИ В TELEGRAM",
            height=40,
            fg_color="transparent",
            hover_color=C["panel2"],
            border_width=1,
            border_color=C["line"],
            text_color=C["ink"],
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=12,
            command=self._login_telegram_user,
        )
        self.tg_user_btn.pack(fill="x", padx=16, pady=(0, 8))
        self.session_label = ctk.CTkLabel(
            left,
            text=self._short_session(),
            font=ctk.CTkFont(size=11),
            text_color=C["muted"],
            anchor="w",
        )
        self.session_label.pack(fill="x", padx=16, pady=(0, 16))

        self._h(left, "НАСТРОЙКИ")
        ctk.CTkLabel(
            left,
            text="Интервал проверки (мин)",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self.interval_entry = ctk.CTkEntry(
            left, textvariable=self.interval_var, **self._entry_kw()
        )
        self.interval_entry.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(
            left,
            text="Сколько свежих постов смотреть за цикл (40–80)",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self.parse_entry = ctk.CTkEntry(
            left, textvariable=self.parse_var, **self._entry_kw()
        )
        self.parse_entry.pack(fill="x", padx=16, pady=(0, 8))
        self.headless_check = ctk.CTkCheckBox(
            left,
            text="Скрыть браузер (фоновая работа)",
            variable=self.headless_var,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=C["ink"],
            fg_color=C["accent"],
            hover_color=C["accent2"],
            border_color=C["line"],
            checkmark_color="#1A1228",
            command=self._persist,
        )
        self.headless_check.pack(anchor="w", padx=16, pady=(0, 14))

        self._h(left, "ДОСТУП К БОТУ")
        ctk.CTkLabel(
            left,
            text="Кто написал боту /start — появится здесь.\n"
            "«Разрешить» / «Блок» / «×» удалить запрос.",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
            justify="left",
            wraplength=280,
        ).pack(anchor="w", padx=16, pady=(0, 6))
        self.allowed_frame = ctk.CTkScrollableFrame(
            left,
            fg_color=C["panel2"],
            corner_radius=10,
            border_width=1,
            border_color=C["line"],
            height=160,
        )
        self.allowed_frame.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(
            left,
            text="Вручную (если человек ещё не писал боту)",
            font=ctk.CTkFont(size=11),
            text_color=C["dim"],
        ).pack(anchor="w", padx=16, pady=(0, 4))
        self.allowed_entry = ctk.CTkEntry(
            left, placeholder_text="123456789", **self._entry_kw()
        )
        self.allowed_entry.pack(fill="x", padx=16, pady=(0, 8))
        self.allowed_entry.bind("<Return>", lambda _e: self._add_allowed())
        ctk.CTkButton(
            left,
            text="ДОБАВИТЬ ID",
            height=36,
            fg_color=C["ink"],
            hover_color=C["accent2"],
            text_color="#1A1228",
            font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=10,
            command=self._add_allowed,
        ).pack(fill="x", padx=16, pady=(0, 18))

        right = ctk.CTkFrame(
            body,
            fg_color="transparent",
        )
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_columnconfigure(1, weight=1)
        right.grid_rowconfigure(1, weight=1)

        th_cap = ctk.CTkFrame(right, fg_color=C["panel2"], corner_radius=10, height=36)
        th_cap.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        ctk.CTkLabel(
            th_cap,
            text="THREADS",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=C["ink"],
        ).pack(side="left", padx=14, pady=8)
        tg_cap = ctk.CTkFrame(right, fg_color=C["accent_dim"], corner_radius=10, height=36)
        tg_cap.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=(0, 8))
        ctk.CTkLabel(
            tg_cap,
            text="TELEGRAM",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=C["accent"],
        ).pack(side="left", padx=14, pady=8)

        self.log_box = ctk.CTkTextbox(
            right,
            fg_color=C["log_bg"],
            text_color=C["log_fg"],
            font=ctk.CTkFont(family="Consolas", size=12),
            corner_radius=R,
            border_width=1,
            border_color=C["line"],
            wrap="word",
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.log_box.configure(state="disabled")
        self.tg_log_box = ctk.CTkTextbox(
            right,
            fg_color=C["log_bg"],
            text_color=C["log_fg"],
            font=ctk.CTkFont(family="Consolas", size=12),
            corner_radius=R,
            border_width=1,
            border_color=C["accent_dim"],
            wrap="word",
        )
        self.tg_log_box.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        self.tg_log_box.configure(state="disabled")

        foot = ctk.CTkFrame(shell, fg_color="transparent")
        foot.pack(fill="x", padx=28, pady=(4, 18))
        cols = ctk.CTkFrame(foot, fg_color="transparent")
        cols.pack(fill="x")
        for i in range(4):
            cols.grid_columnconfigure(i, weight=1)
        self.lbl_cycle = self._poster_stat(cols, 0, "01", "CYCLE")
        self.lbl_found = self._poster_stat(cols, 1, "00", "FOUND")
        self.lbl_sent = self._poster_stat(cols, 2, "00", "SENT")
        self.lbl_tag = self._poster_stat(cols, 3, "—", "TOPIC")
        try:
            shell.lift()
        except Exception:
            pass

    def _h(self, parent, title: str) -> None:
        ctk.CTkLabel(
            parent,
            text=title,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=C["accent"],
        ).pack(anchor="w", padx=16, pady=(12, 8))

    def _poster_stat(self, parent, col: int, value: str, title: str) -> ctk.CTkLabel:
        box = ctk.CTkFrame(
            parent,
            fg_color=C["panel"],
            corner_radius=12,
            border_width=1,
            border_color=C["line"],
        )
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 10) if col < 3 else (0, 0))
        val = ctk.CTkLabel(
            box,
            text=value,
            font=ctk.CTkFont(family="Arial Black", size=26, weight="bold"),
            text_color=C["ink"],
        )
        val.pack(anchor="w", padx=16, pady=(10, 0))
        ctk.CTkLabel(
            box,
            text=title,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=C["muted"],
        ).pack(anchor="w", padx=16, pady=(0, 12))
        return val

    def _hex_mix(self, a: str, b: str, t: float) -> str:
        t = max(0.0, min(1.0, t))

        def rgb(c: str) -> tuple[int, int, int]:
            h = c.lstrip("#")
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

        ar, ag, ab = rgb(a)
        br, bg, bb = rgb(b)
        return "#{:02x}{:02x}{:02x}".format(
            int(ar + (br - ar) * t),
            int(ag + (bg - ag) * t),
            int(ab + (bb - ab) * t),
        )

    def _seed_orbs(self) -> None:
        self._orbs = []
        for _ in range(22):
            self._orbs.append(
                {
                    "x": random.random(),
                    "y": random.random(),
                    "r": random.randint(22, 90),
                    "ph": random.random() * math.tau,
                    "sp": 0.18 + random.random() * 0.28,
                    "c": random.choice(
                        ("#14101C", "#1A1528", "#121018", "#201830", "#0E0C14")
                    ),
                }
            )

    def _tick_motion(self) -> None:
        if not self.winfo_exists():
            return
        self._anim_t += 0.038
        try:
            w = max(self.winfo_width(), 800)
            h = max(self.winfo_height(), 600)
        except Exception:
            self.after(33, self._tick_motion)
            return
        self.fog.delete("all")
        self.fog.configure(width=w, height=h)
        t = self._anim_t
        for orb in self._orbs:
            x = (orb["x"] + math.sin(t * orb["sp"] + orb["ph"]) * 0.04) * w
            y = (orb["y"] + math.cos(t * orb["sp"] * 0.8 + orb["ph"]) * 0.05) * h
            r = orb["r"]
            self.fog.create_oval(x - r, y - r, x + r, y + r, outline="", fill=orb["c"])
        if self._status_key in {"running", "scanning", "starting"}:
            pulse = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * 2.4))
            try:
                self.status_pill.configure(
                    text_color=self._hex_mix(C["accent_dim"], C["accent2"], pulse)
                )
            except Exception:
                pass
        self.after(33, self._tick_motion)

    def _paint_fog(self) -> None:
        self._seed_orbs()

    def _short_session(self) -> str:
        from tg_user_auth import session_authorized

        th = "THREADS OK" if session_exists() else "THREADS нет"
        tg = "TG-группы OK" if session_authorized() else "TG-группы нет входа"
        return f"{th} · {tg}"

    def _persist(self) -> None:
        try:
            interval = max(1, int(self.interval_var.get().strip() or "2"))
        except ValueError:
            interval = 2
            self.interval_var.set("2")
        try:
            parse_posts = max(1, min(200, int(self.parse_var.get().strip() or "40")))
        except ValueError:
            parse_posts = 40
            self.parse_var.set("40")
        self.ui_cfg = UISettings(
            hashtags=list(self._hashtags),
            interval_minutes=interval,
            parse_posts=parse_posts,
            headless=bool(self.headless_var.get()),
            allowed_tg_ids=list(self._allowed_ids),
            geometry=self.geometry(),
        )
        save_ui_settings(self.ui_cfg)
        self._sync_allowed_to_runtime()

    def _sync_allowed_to_runtime(self) -> None:
        ids = tuple(self._allowed_ids)
        if self.settings is not None:
            from dataclasses import replace

            self.settings = replace(self.settings, allowed_tg_ids=ids)
        if self.worker is not None and self.worker.is_running():
            from dataclasses import replace

            self.worker.settings = replace(self.worker.settings, allowed_tg_ids=ids)

    def _user_label(self, user: dict, *, max_len: int = 22) -> str:
        uname = (user.get("username") or "").strip()
        name = (user.get("display_name") or "").strip()
        uid = int(user.get("user_id") or 0)
        if uname:
            text = f"@{uname}"
        elif name:
            text = name
        else:
            text = f"id {uid}"
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text

    def _make_access_row(
        self,
        *,
        uid: int,
        label: str,
        allowed: bool,
    ) -> None:
        row = ctk.CTkFrame(self.allowed_frame, fg_color=C["chip"], corner_radius=8)
        row.pack(fill="x", padx=1, pady=1)

        # Кнопки первыми справа — иначе длинный ник их выталкивает
        actions = ctk.CTkFrame(row, fg_color="transparent", width=150)
        actions.pack(side="right", padx=4, pady=4)
        actions.pack_propagate(False)

        ctk.CTkButton(
            actions,
            text="×",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=C["panel2"],
            text_color=C["muted"],
            font=ctk.CTkFont(size=16, weight="bold"),
            corner_radius=0,
            command=lambda i=uid: self._delete_access_request(i),
        ).pack(side="right", padx=(2, 0))

        if allowed:
            ctk.CTkButton(
                actions,
                text="Блок",
                width=56,
                height=28,
                fg_color="transparent",
                hover_color=C["panel2"],
                border_width=1,
                border_color=C["line"],
                text_color=C["muted"],
                font=ctk.CTkFont(size=11, weight="bold"),
                corner_radius=0,
                command=lambda i=uid: self._remove_allowed(i),
            ).pack(side="right", padx=2)
        else:
            ctk.CTkButton(
                actions,
                text="Разрешить",
                width=90,
                height=28,
                fg_color=C["ink"],
                hover_color="#D0D0D0",
                text_color="#000000",
                font=ctk.CTkFont(size=11, weight="bold"),
                corner_radius=0,
                command=lambda i=uid: self._allow_user(i),
            ).pack(side="right", padx=2)

        status = "OK" if allowed else "ждёт"
        status_color = C["ink"] if allowed else C["muted"]
        ctk.CTkLabel(
            row,
            text=status,
            width=40,
            text_color=status_color,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="left", padx=(8, 0), pady=6)

        ctk.CTkLabel(
            row,
            text=label,
            text_color=C["ink"],
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).pack(side="left", fill="x", expand=True, padx=8, pady=6)

    def _access_fingerprint(self) -> tuple:
        started: list[dict] = []
        if self._db is not None:
            try:
                started = self._db.all_started_users()
            except Exception:
                started = []
        parts = []
        for u in started:
            uid = int(u["user_id"])
            parts.append(
                (
                    uid,
                    uid in self._allowed_ids,
                    u.get("username") or "",
                    u.get("display_name") or "",
                )
            )
        started_ids = {int(u["user_id"]) for u in started}
        orphans = tuple(
            uid for uid in self._allowed_ids if uid not in started_ids
        )
        return (tuple(parts), orphans)

    def _poll_access_users(self) -> None:
        try:
            fp = self._access_fingerprint()
            if fp != getattr(self, "_access_fp", None):
                self._access_fp = fp
                self._render_allowed()
        except Exception:
            pass
        self.after(2500, self._poll_access_users)

    def _render_allowed(self) -> None:
        self._access_fp = self._access_fingerprint()
        for child in self.allowed_frame.winfo_children():
            child.destroy()

        started: list[dict] = []
        if self._db is not None:
            try:
                started = self._db.all_started_users()
            except Exception:
                started = []

        started_ids = {int(u["user_id"]) for u in started}
        orphans = [uid for uid in self._allowed_ids if uid not in started_ids]

        if not started and not orphans:
            ctk.CTkLabel(
                self.allowed_frame,
                text="пока никто не писал боту /start",
                text_color=C["dim"],
                height=32,
            ).pack(anchor="w", padx=8, pady=8)
            return

        for user in started:
            uid = int(user["user_id"])
            self._make_access_row(
                uid=uid,
                label=self._user_label(user),
                allowed=uid in self._allowed_ids,
            )

        for uid in orphans:
            self._make_access_row(
                uid=uid,
                label=f"id {uid}",
                allowed=True,
            )

    def _allow_user(self, uid: int) -> None:
        uid = int(uid)
        if uid not in self._allowed_ids:
            self._allowed_ids.append(uid)
            self._append_log(f"Доступ + {uid}")
            self._render_allowed()
            self._persist()

    def _delete_access_request(self, uid: int) -> None:
        """Убрать запрос из списка (и из белого списка, если был)."""
        uid = int(uid)
        removed_allow = uid in self._allowed_ids
        if removed_allow:
            self._allowed_ids = [i for i in self._allowed_ids if i != uid]
        deleted = False
        if self._db is not None:
            try:
                deleted = self._db.delete_user(uid)
            except Exception as exc:
                self._append_log(f"Не удалось удалить запрос {uid}: {exc}")
                return
        if deleted or removed_allow:
            self._append_log(f"Запрос удалён · {uid}")
            self._render_allowed()
            if removed_allow:
                self._persist()
            else:
                self._access_fp = None

    def _add_allowed(self) -> None:
        raw = (self.allowed_entry.get() or "").strip()
        ids = normalize_tg_ids(raw)
        if not ids:
            messagebox.showinfo("ID", "Введи числовой Telegram ID, например 123456789")
            return
        added = False
        for uid in ids:
            if uid not in self._allowed_ids:
                self._allowed_ids.append(uid)
                added = True
                self._append_log(f"Доступ + {uid}")
        self.allowed_entry.delete(0, "end")
        if added:
            self._render_allowed()
            self._persist()

    def _remove_allowed(self, uid: int) -> None:
        before = len(self._allowed_ids)
        self._allowed_ids = [i for i in self._allowed_ids if i != uid]
        if len(self._allowed_ids) != before:
            self._append_log(f"Доступ − {uid}")
            self._render_allowed()
            self._persist()

    def _render_chips(self) -> None:
        for child in self.chips_frame.winfo_children():
            child.destroy()
        if not self._hashtags:
            ctk.CTkLabel(
                self.chips_frame,
                text="  —  ",
                text_color=C["dim"],
                height=32,
            ).pack(anchor="w", padx=8, pady=8)
            return
        for tag in self._hashtags:
            row = ctk.CTkFrame(self.chips_frame, fg_color=C["chip"], corner_radius=8)
            row.pack(fill="x", padx=1, pady=1)
            ctk.CTkLabel(
                row,
                text=display_hashtag(tag),
                text_color=C["ink"],
                font=ctk.CTkFont(size=13, weight="bold"),
            ).pack(side="left", padx=10, pady=7)
            ctk.CTkButton(
                row,
                text="×",
                width=28,
                height=26,
                fg_color="transparent",
                hover_color=C["line"],
                text_color=C["muted"],
                corner_radius=0,
                command=lambda t=tag: self._remove_hashtag(t),
            ).pack(side="right", padx=4)

    def _add_hashtag(self) -> None:
        raw = self.tag_entry.get().strip()
        if not raw:
            return
        added_any = False
        for tag in normalize_hashtags(raw):
            if any(t.casefold() == tag.casefold() for t in self._hashtags):
                continue
            self._hashtags.append(tag)
            added_any = True
        self.tag_entry.delete(0, "end")
        self._render_chips()
        self._persist()
        if added_any:
            self._sync_hashtags_to_worker()

    def _remove_hashtag(self, tag: str) -> None:
        self._hashtags = [t for t in self._hashtags if t.casefold() != tag.casefold()]
        self._render_chips()
        self._persist()
        self._sync_hashtags_to_worker()

    def _sync_hashtags_to_worker(self) -> None:
        if not (self.worker and self.worker.is_running()):
            return
        added, removed = self.worker.update_hashtags(self._hashtags)
        if added:
            self._append_log(
                "LIVE ADD · " + ", ".join(display_hashtag(t) for t in added)
            )
        if removed:
            self._append_log(
                "LIVE REMOVE · " + ", ".join(display_hashtag(t) for t in removed)
            )

    def _boot_log(self) -> None:
        if not self.settings:
            self._append_log("Заполни .env и перезапусти.", source="both")
            return
        self._append_log(f"{APP_NAME} готов", source="both")
        tags = ", ".join(display_hashtag(t) for t in self._hashtags) or "—"
        self._append_log(f"Теги    {tags}", source="threads")
        self._append_log(f"Лимит   свежие ×{self.parse_var.get()}", source="threads")
        self._append_log(
            "Браузер скрыт" if self.headless_var.get() else "Браузер видимый",
            source="threads",
        )
        ids = ", ".join(str(i) for i in self._allowed_ids) or "пусто (бот закрыт)"
        self._append_log(
            "Telegram-группы: чек каждые 30с, старт вместе с Threads (START)",
            source="tg",
        )
        self._append_log(f"Доступ  {ids}", source="tg")
        self._append_log("Фильтр  только русский (не украинский) + DeepSeek", source="both")
        self._append_log(self._short_session(), source="both")
        self._append_log(
            "Можно: /add /del теги в боте"
            if session_exists()
            else "Сначала ВОЙТИ В THREADS",
            source="threads",
        )

    def _stop_tg_bot(self) -> None:
        if not self.tg_bot:
            return
        try:
            self.tg_bot.stop()
        except Exception:
            pass
        self.tg_bot = None

    def _start_tg_bot_when_idle(self) -> None:
        if not self.winfo_exists():
            return
        if self._busy_login:
            self.after(2000, self._start_tg_bot_when_idle)
            return
        from tg_user_auth import session_authorized

        # Бот команд (теги/группы) работает и без Telethon-сессии
        self._start_tg_bot()
        if not session_authorized():
            return

    def _start_tg_bot(self) -> None:
        if not self.settings:
            return
        if self._busy_login:
            return
        if self.tg_bot:
            return
        try:
            db = self._db or PostDatabase(self.settings.database_path)
            self._db = db

            def on_tags_changed(added: list[str], removed: list[str]) -> None:
                def apply() -> None:
                    changed = False
                    for tag in added:
                        if not any(
                            t.casefold() == tag.casefold() for t in self._hashtags
                        ):
                            self._hashtags.append(tag)
                            changed = True
                            self._append_log(f"GUI + {display_hashtag(tag)}")

                    # Удаляем из GUI, если больше никто не подписан на тег
                    still = {t.casefold() for t in db.all_subscribed_tags()}
                    for tag in removed:
                        key = tag.casefold()
                        if key in still:
                            continue
                        before = len(self._hashtags)
                        self._hashtags = [
                            t for t in self._hashtags if t.casefold() != key
                        ]
                        if len(self._hashtags) != before:
                            changed = True
                            self._append_log(f"GUI − {display_hashtag(tag)}")

                    if changed:
                        self._render_chips()
                        self._persist()
                        if self.worker and self.worker.is_running():
                            self.worker.update_hashtags(self._hashtags)

                self.after(0, apply)

            def on_groups_changed(added: list[str], removed: list[str]) -> None:
                def apply() -> None:
                    if added:
                        self._append_log(
                            f"GUI + группы: {', '.join(added)}", source="tg"
                        )
                    if removed:
                        self._append_log(
                            f"GUI − группы: {', '.join(removed)}", source="tg"
                        )
                    # settings уже обновлены ботом; перезапуск слушателя
                    if self.worker and self.worker.is_running():
                        self._start_tg_user()
                    elif added or removed:
                        self._append_log(
                            "группы сохранены — запусти START, чтобы слушать",
                            source="tg",
                        )

                self.after(0, apply)

            def on_user_seen(uid: int) -> None:
                self.after(0, self._render_allowed)

            self.tg_bot = TelegramControlBot(
                self.settings,
                db,
                on_tags_changed=on_tags_changed,
                on_groups_changed=on_groups_changed,
                on_log=lambda m: self.after(
                    0, lambda msg=m: self._append_log(msg, source="tg")
                ),
                get_allowed_ids=lambda: set(self._allowed_ids),
                on_user_seen=on_user_seen,
            )
            self.tg_bot.start()
        except Exception as exc:
            self._append_log(f"Бот команд не стартовал: {exc}")

    def _close_qr_win(self) -> None:
        win = getattr(self, "_qr_win", None)
        self._qr_win = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _show_qr(self, url: str) -> None:
        try:
            import qrcode
            from PIL import Image
        except Exception:
            self._append_log(f"QR URL: {url}", source="tg")
            return
        img = qrcode.make(url)
        if not isinstance(img, Image.Image):
            img = img.get_image()
        img = img.convert("RGB").resize((260, 260))
        photo = ctk.CTkImage(light_image=img, dark_image=img, size=(260, 260))
        win = getattr(self, "_qr_win", None)
        if win is None or not win.winfo_exists():
            win = ctk.CTkToplevel(self)
            win.title("Вход Telegram")
            win.geometry("320x420")
            win.configure(fg_color=C["panel"])
            win.attributes("-topmost", True)
            ctk.CTkLabel(
                win,
                text="Наведи камеру Telegram",
                font=ctk.CTkFont(size=14, weight="bold"),
                text_color=C["ink"],
            ).pack(pady=(16, 6))
            ctk.CTkLabel(
                win,
                text="Настройки → Устройства → Подключить",
                font=ctk.CTkFont(size=11),
                text_color=C["muted"],
            ).pack()
            lbl = ctk.CTkLabel(win, text="")
            lbl.pack(pady=12)
            self._qr_label = lbl
            self._qr_win = win
            win.protocol("WM_DELETE_WINDOW", self._close_qr_win)
        self._qr_photo = photo
        self._qr_label.configure(image=photo, text="")

    def _ask_string(
        self, title: str, prompt: str, *, show: str | None = None, wait: float = 300
    ) -> str:
        box: dict[str, str] = {"v": ""}
        done = threading.Event()

        def show_dlg() -> None:
            win = ctk.CTkToplevel(self)
            win.title(title)
            win.geometry("420x280")
            win.configure(fg_color=C["panel"])
            win.attributes("-topmost", True)
            win.resizable(False, False)
            ctk.CTkLabel(
                win,
                text=title,
                font=ctk.CTkFont(size=16, weight="bold"),
                text_color=C["ink"],
            ).pack(pady=(18, 8), padx=20)
            ctk.CTkLabel(
                win,
                text=prompt,
                font=ctk.CTkFont(size=12),
                text_color=C["muted"],
                wraplength=360,
                justify="left",
            ).pack(padx=20, pady=(0, 12))
            entry = ctk.CTkEntry(win, **self._entry_kw(height=40))
            if show:
                entry.configure(show=show)
            entry.pack(fill="x", padx=20, pady=(0, 14))
            entry.focus_set()

            def accept() -> None:
                box["v"] = (entry.get() or "").strip()
                try:
                    win.grab_release()
                except Exception:
                    pass
                win.destroy()
                done.set()

            def cancel() -> None:
                box["v"] = ""
                try:
                    win.grab_release()
                except Exception:
                    pass
                win.destroy()
                done.set()

            ctk.CTkButton(
                win,
                text="ОК",
                height=40,
                fg_color=C["ink"],
                hover_color=C["accent2"],
                text_color="#1A1228",
                font=ctk.CTkFont(size=13, weight="bold"),
                command=accept,
            ).pack(fill="x", padx=20, pady=(0, 16))
            entry.bind("<Return>", lambda _e: accept())
            win.protocol("WM_DELETE_WINDOW", cancel)
            try:
                win.grab_set()
                win.lift()
                win.focus_force()
            except Exception:
                pass
            win.after(200, lambda: (win.lift(), entry.focus_set()))

        self.after(0, show_dlg)
        done.wait(wait)
        return box["v"]

    def _login_telegram_user(self) -> None:
        from config import load_settings
        from tg_user_auth import normalize_phone, phone_format_hint

        if not self.settings:
            messagebox.showerror("Config", "Сначала заполни .env")
            return
        if self.worker and self.worker.is_running():
            messagebox.showwarning("Занято", "Сначала STOP")
            return
        if self._busy_login:
            return

        phone = normalize_phone(self.tg_phone_entry.get() or "")
        if not phone:
            saved = (getattr(self.settings, "telegram_user_phone", "") or "").strip()
            phone = normalize_phone(saved)
        hint = phone_format_hint(phone)
        if hint:
            messagebox.showerror("Telegram", hint)
            return
        if not phone.startswith("+") or len(phone) < 11:
            messagebox.showerror(
                "Telegram",
                "Введи номер как в Telegram → Настройки: +код страны и цифры",
            )
            return

        self._stop_tg_user()
        self._stop_tg_bot()
        self._busy_login = True
        self._set_controls(running=False)
        self.hint_label.configure(text="ШЛЮ КОД", text_color=C["ink"])
        self._append_log(
            "Вход Telegram по номеру — код смотри в чате Telegram (777000), не SMS",
            source="tg",
        )

        def finish(err: str) -> None:
            self._busy_login = False
            self._set_controls(running=False)
            self._close_qr_win()
            self.after(1500, self._start_tg_bot_when_idle)
            if err:
                from tg_user_auth import _readable_err

                err = _readable_err(err)
                messagebox.showerror("Telegram", err)
                self._append_log(f"Telegram: {err}", source="tg")
                self.hint_label.configure(text="Вход не удался", text_color=C["muted"])
                return
            try:
                self.settings = load_settings()
            except Exception:
                pass
            self.session_label.configure(text=self._short_session())
            if self.worker and self.worker.is_running():
                self._start_tg_user()
                self._append_log("Telegram: вход ок, слушаю группы", source="tg")
            else:
                self._append_log(
                    "Telegram: вход ок · группы подключу на START вместе с Threads",
                    source="tg",
                )
            self.hint_label.configure(text="TELEGRAM OK", text_color=C["ink"])

        def job() -> None:
            from tg_user_auth import finish_login, last_code_hint, send_login_code

            err = send_login_code(phone)
            if err:
                self.after(0, lambda e=err: finish(e))
                return
            self.after(
                0,
                lambda: self.hint_label.configure(text="ЖДУ КОД", text_color=C["ink"]),
            )
            self.after(
                0,
                lambda: self._append_log(
                    "Код: чат Telegram (777000) на телефоне. Если пусто — SMS. Это не звонок.",
                    source="tg",
                ),
            )
            code = self._ask_string(
                "Код Telegram",
                last_code_hint() + "\n\nВставь код сюда:",
            )
            if not code.strip():
                self.after(0, lambda: finish("Нет кода — нажми вход ещё раз"))
                return
            self.after(
                0,
                lambda: self.hint_label.configure(text="ВХОЖУ", text_color=C["ink"]),
            )
            self.after(
                0,
                lambda: self._append_log(
                    "Проверяю код… это не строки про бота в чёрном окне",
                    source="tg",
                ),
            )
            err = finish_login(code)
            if err == "2FA":
                pwd = self._ask_string(
                    "2FA",
                    "Облачный пароль Telegram",
                    show="*",
                    wait=180,
                )
                if not pwd.strip():
                    self.after(0, lambda: finish("Нужен облачный пароль 2FA"))
                    return
                err = finish_login("", pwd)
            self.after(0, lambda e=err: finish(e))

        threading.Thread(target=job, name="tg-phone-login", daemon=True).start()

    def _stop_tg_user(self) -> None:
        if not self.tg_user:
            return
        try:
            self.tg_user.stop()
        except Exception:
            pass
        self.tg_user = None

    def _start_tg_user(self) -> None:
        if not self.settings:
            return
        self._stop_tg_user()
        if not getattr(self.settings, "telegram_user_enabled", False):
            self._append_log(
                "TELEGRAM_USER_ENABLED выключен — группы не слушаю",
                source="tg",
            )
            return
        from tg_user_auth import session_authorized, session_file_ready

        if not session_authorized():
            self._append_log(
                "нет сессии Telegram — нажми «ВОЙТИ В TELEGRAM»",
                source="tg",
            )
            return
        if not session_file_ready():
            self._append_log("файл сессии пустой — войди заново", source="tg")
            return
        try:
            db = self._db or PostDatabase(self.settings.database_path)
            self._db = db
            self.tg_user = TelegramUserSource(
                self.settings,
                db,
                on_log=lambda m: self.after(
                    0, lambda msg=m: self._append_log(msg, source="tg")
                ),
            )
            self.tg_user.start()
        except Exception as exc:
            self._append_log(f"слушатель групп не стартовал: {exc}", source="tg")

    def _guess_log_source(self, message: str) -> str:
        low = (message or "").lstrip().casefold()
        if low.startswith(
            (
                "tg",
                "telegram",
                "telethon",
                "бот ",
                "аккаунт @",
                "подключаюсь",
                "слушаю",
                "пост ·",
                "пульс",
                "догоняю",
                "источник",
                "лид →",
                "skip ·",
            )
        ):
            return "tg"
        if "войти в telegram" in low or "вход telegram" in low:
            return "tg"
        return "threads"

    def _append_log(self, message: str, source: str | None = None) -> None:
        from models import redact_secrets

        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"{stamp}   {redact_secrets(message)}\n"
        if source is None:
            source = self._guess_log_source(message)
        boxes = []
        if source in {"threads", "both"}:
            boxes.append(self.log_box)
        if source in {"tg", "both"}:
            boxes.append(getattr(self, "tg_log_box", self.log_box))
        for box in boxes:
            box.configure(state="normal")
            box.insert("end", text)
            box.see("end")
            box.configure(state="disabled")

    def _set_status(self, key: str) -> None:
        self._status_key = key
        label, color = STATUS.get(key, STATUS["idle"])
        self.status_pill.configure(text=label, text_color=color)

    def _set_controls(self, *, running: bool) -> None:
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.login_btn.configure(
            state="disabled" if (running or self._busy_login) else "normal"
        )
        self.tg_user_btn.configure(
            state="disabled" if (running or self._busy_login) else "normal"
        )
        if getattr(self, "tg_phone_entry", None) is not None:
            self.tg_phone_entry.configure(
                state="disabled" if (running or self._busy_login) else "normal"
            )

    def _on_log(self, message: str) -> None:
        self.after(0, lambda m=message: self._append_log(m, source="threads"))

    def _on_stats(self, stats: WorkerStats) -> None:
        def apply() -> None:
            self._set_status(stats.status)
            self.lbl_cycle.configure(text=f"{stats.cycle:02d}")
            self.lbl_found.configure(text=f"{stats.found:02d}")
            self.lbl_sent.configure(text=f"{stats.sent:02d}")
            topic = (stats.last_hashtag or "—").lstrip("#")
            if len(topic) > 14:
                topic = topic[:12] + "…"
            self.lbl_tag.configure(text=topic.upper() if topic != "—" else "—")
            running = stats.status not in {"idle", "error"}
            self._set_controls(running=running)
            if stats.ready_to_publish:
                self.hint_label.configure(text="ЖДУ НОВЫЕ ЛИДЫ", text_color=C["ink"])
            elif running:
                self.hint_label.configure(text="РАБОТАЕТ · STOP — стоп", text_color=C["muted"])
            else:
                self.hint_label.configure(
                    text="ВОЙТИ → START → ждём лиды", text_color=C["dim"]
                )

        self.after(0, apply)

    def _login_threads(self) -> None:
        if not self.settings:
            messagebox.showerror("Config", "Сначала заполни .env")
            return
        if self.worker and self.worker.is_running():
            messagebox.showwarning("Занято", "Сначала STOP")
            return
        if self._busy_login:
            return

        self._busy_login = True
        self._set_controls(running=False)
        self.login_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self._append_log("Открываю браузер — войди в Threads")
        self.hint_label.configure(text="БРАУЗЕР ОТКРЫТ", text_color=C["ink"])

        def job() -> None:
            try:
                from scraper import run_login_only

                run_login_only(self.settings)
                self.after(0, lambda: self._append_log("Сессия сохранена · жми START"))
            except Exception as exc:
                err = str(exc)
                self.after(0, lambda e=err: self._append_log(f"Ошибка входа: {e}"))
            finally:
                self.after(0, self._after_login)

        threading.Thread(target=job, name="threads-login", daemon=True).start()

    def _after_login(self) -> None:
        self._busy_login = False
        ok = session_exists()
        self.session_label.configure(
            text=self._short_session(),
            text_color=C["ink"] if ok else C["muted"],
        )
        self._set_controls(running=False)
        self.hint_label.configure(
            text="Сессия OK · START" if ok else "Вход не удался",
            text_color=C["ink"] if ok else C["muted"],
        )

    def _start(self) -> None:
        if not self.settings:
            messagebox.showerror("Config", "Сначала заполни .env")
            return
        if self._busy_login:
            # Автостарт: не блокируем диалогом — повторим позже
            self.after(2500, self._start)
            return
        if not session_exists():
            self._append_log("Нет сессии Threads — жду вход…")
            self.after(4000, self._start)
            return
        if self.worker and self.worker.is_running():
            return
        if not self._hashtags:
            messagebox.showwarning("Теги", "Добавь хотя бы один тег (или /add в боте)")
            return
        try:
            interval = int(self.interval_var.get().strip())
            if interval < 1:
                raise ValueError
        except ValueError:
            interval = 1
            self.interval_var.set("1")
        try:
            parse_posts = int(self.parse_var.get().strip())
            if parse_posts < 1 or parse_posts > 200:
                raise ValueError
        except ValueError:
            parse_posts = 80
            self.parse_var.set("80")
        # Не даём UI оставить 5 постов, но и не гоним 60+ — это 429.
        parse_posts = max(parse_posts, 15)
        parse_posts = min(parse_posts, 30)
        self.parse_var.set(str(parse_posts))
        interval = max(interval, 8)
        self.interval_var.set(str(interval))

        self._persist()
        try:
            self.settings = load_settings()
        except ValueError as exc:
            messagebox.showerror("Config", str(exc))
            return

        # Галочка важнее кэша .env — браузер скрываем именно по UI
        from dataclasses import replace

        hide = bool(self.headless_var.get())
        self.settings = replace(
            self.settings,
            playwright_headless=hide,
            allowed_tg_ids=tuple(self._allowed_ids),
            parse_posts=parse_posts,
            check_interval_minutes=interval,
        )

        self.worker = ParserWorker(
            settings=self.settings,
            hashtags=self._hashtags,
            interval_minutes=interval,
            parse_posts=parse_posts,
            on_status=self._on_log,
            on_stats=self._on_stats,
        )
        try:
            self.worker.start(self._hashtags, interval, parse_posts)
        except Exception as exc:
            messagebox.showerror("START", str(exc))
            self._append_log(f"START не удался: {exc}")
            return

        self._set_controls(running=True)
        self._set_status("starting")
        mode = "скрытый" if hide else "видимый"
        self._append_log(f"START · свежие ×{parse_posts} · браузер {mode}")
        self._start_tg_bot()
        self._start_tg_user()

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self._stop_tg_user()
        self._set_controls(running=False)
        self._set_status("stopping")
        self._append_log("Остановка…")
        self._append_log("Telegram-группы остановлены", source="tg")

    def _pulse_heartbeat(self) -> None:
        try:
            from heartbeat import touch

            if self.worker and self.worker.is_running():
                st = getattr(self.worker.stats, "status", "") or "running"
                msg = getattr(self.worker.stats, "message", "") or ""
                # Не progress — иначе маскируем зависший поиск
                touch(st, extra=msg, progress=False)
            else:
                touch("gui-alive", progress=False)
        except Exception:
            pass
        if self.winfo_exists():
            self.after(20000, self._pulse_heartbeat)

    def _ensure_watchdog(self) -> None:
        try:
            import subprocess
            import sys
            from pathlib import Path

            lock = Path(__file__).resolve().parent / "watchdog.lock"
            if lock.exists():
                try:
                    old = int(lock.read_text(encoding="utf-8").strip())
                    os.kill(old, 0)
                    return
                except (ValueError, OSError):
                    pass
            py = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
            exe = str(py if py.exists() else sys.executable)
            proc = subprocess.Popen(
                [exe, str(Path(__file__).resolve().parent / "watchdog.py")],
                cwd=str(Path(__file__).resolve().parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            lock.write_text(str(proc.pid), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._persist()
        try:
            from heartbeat import touch

            touch("stopped")
        except Exception:
            pass
        if self.tg_bot:
            try:
                self.tg_bot.stop()
            except Exception:
                pass
        if self.tg_user:
            try:
                self.tg_user.stop()
            except Exception:
                pass
        if self.worker and self.worker.is_running():
            self.worker.stop()
            self.worker._stop.wait(2)
        self.destroy()


def run_gui() -> int:
    from instance_lock import acquire_gui_lock

    if not acquire_gui_lock():
        print("Парсер уже открыт в другом окне. Закрой лишний start.bat.")
        try:
            messagebox.showerror(
                APP_NAME,
                "Парсер уже открыт в другом окне.\n"
                "Оставь одно окно и один start.bat — иначе Telegram бот даёт ошибку 409.",
            )
        except Exception:
            pass
        return 2
    try:
        app = SeasideApp()
        app.mainloop()
    except Exception as exc:
        print(f"GUI crash: {type(exc).__name__}: {exc}")
        raise
    return 0


if __name__ == "__main__":
    run_gui()
