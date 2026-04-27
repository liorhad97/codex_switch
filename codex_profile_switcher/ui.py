from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

from .launcher import (
    DEFAULT_CODEX_USER_DATA_DIR,
    build_codex_launch_command,
    is_safe_codex_user_data_dir,
    launch_codex,
    reveal_in_finder,
)
from .models import AccountRecord, SwitcherConfig, format_status, format_timestamp
from .store import ProfileStore


BG = "#071018"
PANEL = "#0D1721"
PANEL_ALT = "#101D29"
CARD = "#112130"
CARD_SELECTED = "#17314A"
CARD_PRIMARY = "#13352B"
BORDER = "#1F3243"
TEXT = "#E5F0FA"
TEXT_MUTED = "#8FA6B8"
TEXT_SUBTLE = "#6B8294"
ACCENT = "#4CC9A6"
ACCENT_STRONG = "#35B58F"
WARNING = "#FFB55C"
DANGER = "#FF6B73"


class ScrollableFrame(tk.Frame):
    def __init__(self, master: tk.Misc, *, bg: str) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable = tk.Frame(self.canvas, bg=bg)

        self.scrollable.bind(
            "<Configure>",
            lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.window_id = self.canvas.create_window((0, 0), window=self.scrollable, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self.window_id, width=event.width),
        )

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")


class AccountSwitcherApp:
    def __init__(self, root: tk.Tk, store: ProfileStore | None = None) -> None:
        self.root = root
        self.store = store or ProfileStore()
        self.paths = self.store.paths
        self.accounts: list[AccountRecord] = []
        self.config: SwitcherConfig = self.store.load_config()
        self.selected_account_id: str | None = None

        self.title_font = tkfont.Font(family="Avenir Next", size=22, weight="bold")
        self.heading_font = tkfont.Font(family="Avenir Next", size=16, weight="bold")
        self.badge_font = tkfont.Font(family="Menlo", size=10, weight="bold")
        self.body_font = tkfont.Font(family="Avenir Next", size=12)
        self.small_font = tkfont.Font(family="Avenir Next", size=11)

        self._configure_window()
        self._build_shell()
        self.refresh_accounts()

    def _configure_window(self) -> None:
        self.root.title("codex switch")
        self.root.configure(bg=BG)
        self.root.geometry("1200x760")
        self.root.minsize(980, 640)

    def _build_shell(self) -> None:
        container = tk.Frame(self.root, bg=BG)
        container.pack(fill="both", expand=True, padx=20, pady=20)

        self.sidebar = tk.Frame(container, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.sidebar.pack(side="left", fill="y")

        self.content = tk.Frame(container, bg=PANEL_ALT, highlightbackground=BORDER, highlightthickness=1)
        self.content.pack(side="left", fill="both", expand=True, padx=(16, 0))

        self._build_sidebar()
        self._build_content()

    def _build_sidebar(self) -> None:
        header = tk.Frame(self.sidebar, bg=PANEL)
        header.pack(fill="x", padx=18, pady=(18, 10))

        tk.Label(
            header,
            text="Inspector",
            fg=TEXT_SUBTLE,
            bg=PANEL,
            font=("Menlo", 10, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Accounts",
            fg=TEXT,
            bg=PANEL,
            font=self.title_font,
        ).pack(anchor="w", pady=(4, 6))
        tk.Label(
            header,
            text="View managed Codex accounts, choose a safe primary profile, and launch Codex against isolated prepared user-data directories.",
            fg=TEXT_MUTED,
            bg=PANEL,
            font=self.small_font,
            justify="left",
            wraplength=300,
        ).pack(anchor="w")

        toolbar = tk.Frame(self.sidebar, bg=PANEL)
        toolbar.pack(fill="x", padx=18, pady=(0, 12))

        self._make_button(toolbar, "Refresh", self.refresh_accounts, accent=True).pack(
            side="left", fill="x", expand=True
        )

        self.sidebar_list = ScrollableFrame(self.sidebar, bg=PANEL)
        self.sidebar_list.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def _build_content(self) -> None:
        self.content_header = tk.Frame(self.content, bg=PANEL_ALT)
        self.content_header.pack(fill="x", padx=24, pady=(24, 12))

        self.detail_container = tk.Frame(self.content, bg=PANEL_ALT)
        self.detail_container.pack(fill="both", expand=True, padx=24, pady=(0, 24))

    def refresh_accounts(self) -> None:
        accounts, config = self.store.load_accounts()
        self.accounts = accounts
        self.config = config

        fallback_selection = (
            self.selected_account_id
            or self.config.last_selected_account_id
            or self.config.primary_account_id
            or (accounts[0].id if accounts else None)
        )
        if fallback_selection and all(account.id != fallback_selection for account in accounts):
            fallback_selection = accounts[0].id if accounts else None
        self.selected_account_id = fallback_selection
        self._render_sidebar_cards()
        self._render_detail()

    def _render_sidebar_cards(self) -> None:
        for child in self.sidebar_list.scrollable.winfo_children():
            child.destroy()

        if not self.accounts:
            empty = tk.Frame(self.sidebar_list.scrollable, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            empty.pack(fill="x", padx=6, pady=6)
            tk.Label(
                empty,
                text="No managed accounts were found under llm_accounts_profiles.",
                fg=TEXT_MUTED,
                bg=CARD,
                font=self.body_font,
                wraplength=280,
                justify="left",
                padx=14,
                pady=14,
            ).pack(fill="x")
            return

        for account in self.accounts:
            selected = account.id == self.selected_account_id
            base_bg = CARD_PRIMARY if account.app_primary else CARD_SELECTED if selected else CARD
            card = tk.Frame(
                self.sidebar_list.scrollable,
                bg=base_bg,
                highlightbackground=ACCENT if selected or account.app_primary else BORDER,
                highlightthickness=1,
                cursor="hand2",
            )
            card.pack(fill="x", padx=6, pady=6)
            card.bind("<Button-1>", lambda _event, account_id=account.id: self._select_account(account_id))

            top = tk.Frame(card, bg=base_bg)
            top.pack(fill="x", padx=12, pady=(12, 8))

            avatar = tk.Canvas(top, width=34, height=34, bg=base_bg, highlightthickness=0, bd=0)
            avatar.create_oval(2, 2, 32, 32, fill=PANEL_ALT, outline=ACCENT if account.app_primary else BORDER)
            avatar.create_text(17, 17, text=account.initial, fill=TEXT, font=("Avenir Next", 13, "bold"))
            avatar.pack(side="left")
            avatar.bind("<Button-1>", lambda _event, account_id=account.id: self._select_account(account_id))

            copy = tk.Frame(top, bg=base_bg)
            copy.pack(side="left", fill="x", expand=True, padx=(10, 0))
            for widget in (
                tk.Label(copy, text=account.title, fg=TEXT, bg=base_bg, font=("Avenir Next", 13, "bold")),
                tk.Label(
                    copy,
                    text=account.subtitle,
                    fg=TEXT_MUTED,
                    bg=base_bg,
                    font=self.small_font,
                    wraplength=210,
                    justify="left",
                ),
            ):
                widget.pack(anchor="w")
                widget.bind("<Button-1>", lambda _event, account_id=account.id: self._select_account(account_id))

            badges = tk.Frame(card, bg=base_bg)
            badges.pack(fill="x", padx=12, pady=(0, 12))
            if account.app_primary:
                self._make_badge(badges, "Primary", bg="#0F4A3C", fg="#B8F6E7").pack(side="left", padx=(0, 6))
            self._make_badge(
                badges,
                format_status(account.status),
                bg="#1C2B39",
                fg=TEXT_MUTED if account.status != "error" else DANGER,
            ).pack(side="left")

    def _render_detail(self) -> None:
        for child in self.content_header.winfo_children():
            child.destroy()
        for child in self.detail_container.winfo_children():
            child.destroy()

        account = self._selected_account()
        if account is None:
            tk.Label(
                self.detail_container,
                text="Select an account from the left sidebar.",
                fg=TEXT_MUTED,
                bg=PANEL_ALT,
                font=self.body_font,
            ).pack(anchor="w")
            return

        title_wrap = tk.Frame(self.content_header, bg=PANEL_ALT)
        title_wrap.pack(fill="x")
        left = tk.Frame(title_wrap, bg=PANEL_ALT)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="Inspector", fg=TEXT_SUBTLE, bg=PANEL_ALT, font=("Menlo", 10, "bold")).pack(anchor="w")
        tk.Label(left, text=account.title, fg=TEXT, bg=PANEL_ALT, font=self.title_font).pack(anchor="w", pady=(4, 4))
        tk.Label(left, text=account.subtitle, fg=TEXT_MUTED, bg=PANEL_ALT, font=self.body_font).pack(anchor="w")

        actions = tk.Frame(title_wrap, bg=PANEL_ALT)
        actions.pack(side="right")
        self._make_button(actions, "Reveal account home", lambda: reveal_in_finder(account.home_dir)).pack(
            side="left", padx=(0, 8)
        )
        if account.mapped_codex_profile:
            self._make_button(
                actions,
                "Open in Codex",
                self._open_selected_in_codex,
                accent=True,
            ).pack(side="left")

        badge_row = tk.Frame(self.detail_container, bg=PANEL_ALT)
        badge_row.pack(anchor="w", pady=(0, 18))
        if account.app_primary:
            self._make_badge(badge_row, "Primary", bg="#0F4A3C", fg="#B8F6E7").pack(side="left", padx=(0, 8))
        if account.flutty_primary and not account.app_primary:
            self._make_badge(badge_row, "Flutty primary", bg="#2B3344", fg=WARNING).pack(side="left", padx=(0, 8))
        self._make_badge(
            badge_row,
            format_status(account.status),
            bg="#1D2937",
            fg=DANGER if account.status == "error" else TEXT_MUTED,
        ).pack(side="left")

        cards_row = tk.Frame(self.detail_container, bg=PANEL_ALT)
        cards_row.pack(fill="both", expand=True)

        info_card = tk.Frame(cards_row, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        info_card.pack(side="left", fill="both", expand=True)
        action_card = tk.Frame(cards_row, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        action_card.pack(side="left", fill="both", expand=True, padx=(14, 0))

        self._render_info_card(info_card, account)
        self._render_action_card(action_card, account)

    def _render_info_card(self, parent: tk.Frame, account: AccountRecord) -> None:
        tk.Label(parent, text="Profile details", fg=TEXT, bg=CARD, font=self.heading_font).pack(
            anchor="w", padx=18, pady=(18, 12)
        )
        source_label = {
            "managed_profile": "managed profile directory",
            "local_created": "switcher local profile",
            "local_oauth": "switcher local profile",
        }.get(account.source, account.source.replace("_", " "))
        self._detail_row(parent, "Account ID", account.id)
        self._detail_row(parent, "Account home", str(account.home_dir))
        self._detail_row(
            parent,
            "Prepared Codex profile",
            str(account.mapped_codex_profile) if account.mapped_codex_profile else "Not mapped yet",
        )
        self._detail_row(parent, "Source", source_label)
        self._detail_row(parent, "Created", format_timestamp(account.created_at))
        self._detail_row(parent, "Updated", format_timestamp(account.updated_at))
        self._detail_row(parent, "Default Codex profile", str(DEFAULT_CODEX_USER_DATA_DIR))

        note = tk.Frame(parent, bg="#0F1C2A", highlightbackground=BORDER, highlightthickness=1)
        note.pack(fill="x", padx=18, pady=(12, 18))
        tk.Label(
            note,
            text=(
                "This app never edits the default Codex cookies, local storage, or Keychain-backed login data. "
                "Use a prepared isolated user-data directory for each account."
            ),
            fg=TEXT_MUTED,
            bg="#0F1C2A",
            font=self.small_font,
            wraplength=420,
            justify="left",
            padx=12,
            pady=12,
        ).pack(fill="x")

        if account.issues:
            issue_frame = tk.Frame(parent, bg="#2A171B", highlightbackground=DANGER, highlightthickness=1)
            issue_frame.pack(fill="x", padx=18, pady=(0, 18))
            tk.Label(
                issue_frame,
                text="\n".join(account.issues),
                fg="#FFCFD3",
                bg="#2A171B",
                font=self.small_font,
                wraplength=420,
                justify="left",
                padx=12,
                pady=12,
            ).pack(fill="x")

    def _render_action_card(self, parent: tk.Frame, account: AccountRecord) -> None:
        tk.Label(parent, text="Actions", fg=TEXT, bg=CARD, font=self.heading_font).pack(
            anchor="w", padx=18, pady=(18, 12)
        )
        tk.Label(
            parent,
            text=(
                "Set primary copies this account's Codex auth into the main Codex home. "
                "You can still launch with an isolated user-data directory if needed."
            ),
            fg=TEXT_MUTED,
            bg=CARD,
            font=self.small_font,
            wraplength=420,
            justify="left",
        ).pack(anchor="w", padx=18)

        button_column = tk.Frame(parent, bg=CARD)
        button_column.pack(fill="x", padx=18, pady=(18, 12))

        if not account.app_primary:
            self._make_button(
                button_column,
                "Set Primary",
                lambda: self._set_primary(account.id),
                accent=True,
            ).pack(fill="x")

        self._make_button(
            button_column,
            "Choose Launch Profile…",
            self._choose_launch_profile,
        ).pack(fill="x", pady=(10, 0))

        self._make_button(
            button_column,
            "Use Suggested Directory",
            self._use_suggested_profile_directory,
        ).pack(fill="x", pady=(10, 0))

        if account.mapped_codex_profile:
            self._make_button(
                button_column,
                "Reveal Launch Profile",
                lambda: reveal_in_finder(account.mapped_codex_profile or account.home_dir),
            ).pack(fill="x", pady=(10, 0))
            self._make_button(
                button_column,
                "Clear Launch Profile",
                self._clear_launch_profile,
            ).pack(fill="x", pady=(10, 0))
            self._make_button(
                button_column,
                "Open in Codex",
                self._open_selected_in_codex,
                accent=True,
            ).pack(fill="x", pady=(10, 0))

        extra = tk.Frame(parent, bg="#0F1C2A", highlightbackground=BORDER, highlightthickness=1)
        extra.pack(fill="x", padx=18, pady=(8, 18))
        tk.Label(
            extra,
            text=(
                "Suggested prepared directory:\n"
                f"{self.paths.prepared_profiles_root / account.id}\n\n"
                "If you point at the default Codex profile, the app will reject it."
            ),
            fg=TEXT_MUTED,
            bg="#0F1C2A",
            font=self.small_font,
            justify="left",
            wraplength=420,
            padx=12,
            pady=12,
        ).pack(fill="x")

    def _detail_row(self, parent: tk.Frame, label: str, value: str) -> None:
        frame = tk.Frame(parent, bg=CARD)
        frame.pack(fill="x", padx=18, pady=4)
        tk.Label(frame, text=label, fg=TEXT_SUBTLE, bg=CARD, font=("Menlo", 10, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text=value,
            fg=TEXT,
            bg=CARD,
            font=self.small_font,
            justify="left",
            wraplength=430,
        ).pack(anchor="w", pady=(2, 0))

    def _select_account(self, account_id: str) -> None:
        self.selected_account_id = account_id
        self.config = self.store.set_selected_account(self.config, account_id)
        self._render_sidebar_cards()
        self._render_detail()

    def _selected_account(self) -> AccountRecord | None:
        return next((account for account in self.accounts if account.id == self.selected_account_id), None)

    def _set_primary(self, account_id: str) -> None:
        try:
            self.config = self.store.set_primary(self.config, account_id)
        except ValueError as error:
            messagebox.showerror("Could not set primary", str(error))
            return
        self.refresh_accounts()

    def _choose_launch_profile(self) -> None:
        account = self._selected_account()
        if account is None:
            return
        selected = filedialog.askdirectory(
            title="Choose prepared Codex user-data directory",
            mustexist=True,
            initialdir=str(self.paths.prepared_profiles_root),
        )
        if not selected:
            return
        self._apply_launch_profile(account, Path(selected))

    def _use_suggested_profile_directory(self) -> None:
        account = self._selected_account()
        if account is None:
            return
        suggested = (self.paths.prepared_profiles_root / account.id).resolve()
        suggested.mkdir(parents=True, exist_ok=True)
        self._apply_launch_profile(account, suggested)

    def _apply_launch_profile(self, account: AccountRecord, candidate: Path) -> None:
        resolved = candidate.expanduser().resolve()
        if not is_safe_codex_user_data_dir(resolved):
            messagebox.showerror(
                "Unsafe profile path",
                (
                    "The selected directory points at the default Codex profile or a nested path inside it.\n\n"
                    f"Rejected path:\n{resolved}\n\n"
                    f"Default path:\n{DEFAULT_CODEX_USER_DATA_DIR}"
                ),
            )
            return
        self.config = self.store.set_launch_profile(self.config, account.id, resolved)
        self.refresh_accounts()

    def _clear_launch_profile(self) -> None:
        account = self._selected_account()
        if account is None:
            return
        self.config = self.store.clear_launch_profile(self.config, account.id)
        self.refresh_accounts()

    def _open_selected_in_codex(self) -> None:
        account = self._selected_account()
        if account is None:
            return
        self.config = self.store.set_primary(self.config, account.id)
        self.refresh_accounts()

        try:
            command = build_codex_launch_command(self.config.codex_app_path)
            launch_codex(self.config.codex_app_path)
        except FileNotFoundError:
            messagebox.showerror(
                "Codex not found",
                f"Codex.app was not found at:\n{self.config.codex_app_path}",
            )
            return
        except Exception as error:  # pragma: no cover - UI error path
            messagebox.showerror("Launch failed", f"Could not launch Codex.\n\n{error}")
            return

        messagebox.showinfo("Launching Codex", "Codex was launched with this command:\n\n" + " ".join(command))

    def _make_badge(self, master: tk.Misc, text: str, *, bg: str, fg: str) -> tk.Label:
        return tk.Label(
            master,
            text=text.upper(),
            bg=bg,
            fg=fg,
            font=self.badge_font,
            padx=10,
            pady=4,
        )

    def _make_button(
        self,
        master: tk.Misc,
        text: str,
        command,
        *,
        accent: bool = False,
    ) -> tk.Button:
        return tk.Button(
            master,
            text=text,
            command=command,
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            activebackground=ACCENT_STRONG if accent else CARD_SELECTED,
            activeforeground=TEXT,
            bg=ACCENT if accent else CARD,
            fg="#041018" if accent else TEXT,
            font=("Avenir Next", 12, "bold"),
            padx=14,
            pady=10,
        )


def main() -> None:
    root = tk.Tk()
    app = AccountSwitcherApp(root)
    del app
    root.mainloop()


if __name__ == "__main__":
    main()
