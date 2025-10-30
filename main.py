#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telethon watcher for @giftmarketfeed that:
- Prompts the user for filters (Collections, Models, Backdrops, Symbols).
- Chooses action: Auto-buy OR Print (to @username/chat_id or console).
- For each t.me/nft/<slug> link, fetches attributes via payments.getUniqueStarGift
  and filters by collection (base slug), model, backdrop, symbol (pattern).
- If matched:
    * Auto-buy: creates InputInvoiceStarGiftResale -> GetPaymentForm -> SendStarsForm
    * Print: prints to console and/or sends to a chosen chat.

Requirements:
  pip install telethon

Env (optional):
  TELEGRAM_API_ID, TELEGRAM_API_HASH
"""

import subprocess
import os
import sys

buy_one = True
done = False

def ensure_telethon_resale_invoice():
    """
    Ensure telethon.tl.types.InputInvoiceStarGiftResale is available.
    If not, run:
      <python> -m pip uninstall telethon
      <python> -m pip install "git+https://github.com/LonamiWebs/Telethon.git@v1#egg=Telethon"
    and then restart the current process.
    """
    try:
        from telethon.tl import types
        if hasattr(types, "InputInvoiceStarGiftResale"):
            return  # all good
    except Exception as e:
        print(f"[warn] telethon import issue: {e}")

    print("[fix] InputInvoiceStarGiftResale не найден; обновляем Telethon…")
    cmds = [
        [sys.executable, "-m", "pip", "uninstall", "-y", "telethon"],
        [sys.executable, "-m", "pip", "install", "git+https://github.com/LonamiWebs/Telethon.git@v1#egg=Telethon"],
    ]
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    for cmd in cmds:
        print(" ".join(cmd))
        proc = subprocess.run(cmd, env=env)
        if proc.returncode != 0:
            raise SystemExit(f"[error] Комманда выполнена с ошибкой: {' '.join(cmd)} (exit {proc.returncode})")

    print("[fix] Telethon обновлен, пожайлуста, перезапустите процесс…")

ensure_telethon_resale_invoice()
import asyncio, re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple, List

from getpass import getpass
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl import types, functions

SESSION_FILE = Path("ssession.txt")
os.system("clear")
CHANNEL = "https://t.me/giftmarketfeed"
LINK_RE = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.IGNORECASE)
# After: LINK_RE = ...
PROFIT_RE = re.compile(r"Окупаемость\s*\([^)]*\)\s*:\s*([+-]?\d+(?:[.,]\d+)?)\s*TON", re.IGNORECASE)


print("""
--- GiftHunter v1.0.1 by @p0slv ---

🕵️‍♀️ Автоматический поиск и покупка подарков по фильтрам!

💵 Этот софт бесплатный, и создан @p0slv. Если вам его продали, потребуйте возврат средств как можно быстрее.
""")

# ----------------------- utilities -----------------------

def _csv_list(s: str) -> List[str]:
    """Split a comma-separated string, trim, drop empties, lower() for case-insensitive contains()."""
    if not s:
        return []
    return [x.strip().lower() for x in s.split(",") if x.strip()]

def _norm(s: Optional[str]) -> str:
    return (s or "").strip()

def _contains_any(haystack: Optional[str], needles: Sequence[str]) -> bool:
    """Case-insensitive substring match: True if any needle is in haystack."""
    if not needles:
        return True  # no filter
    h = (haystack or "").lower()
    return any(n in h for n in needles)

def split_slug_to_base_and_num(full_slug: str) -> Tuple[str, Optional[int]]:
    """
    Accepts e.g. "LunarSnake-116780" -> ("LunarSnake", 116780)
    If no dash/num present, returns (slug, None).
    """
    parts = full_slug.split("-")
    if len(parts) >= 2 and parts[-1].isdigit():
        return "-".join(parts[:-1]), int(parts[-1])
    return full_slug, None

# ----------------------- login helpers -----------------------

def read_session_string() -> Optional[str]:
    if SESSION_FILE.exists():
        s = SESSION_FILE.read_text(encoding="utf-8").strip()
        return s or None
    return None

def write_session_string(s: str) -> None:
    SESSION_FILE.write_text(s, encoding="utf-8")
    print(f"[ok] StringSession saved to {SESSION_FILE.resolve()}")

async def ensure_logged_in(client: TelegramClient) -> None:
    await client.connect()
    if await client.is_user_authorized():
        return
    print("[*] Необходима авторизация.")
    phone = input("Телефон (+15551234567): ").strip()
    code  = input("Код: ").strip()
    twofa = getpass("Пароль (оставьте пустым если не установлен): ")

    try:
        await client.send_code_request(phone)
    except Exception:
        pass

    try:
        await client.sign_in(phone=phone, code=code)
    except Exception as e:
        # Telethon raises SessionPasswordNeededError, but handle generically
        if "PASSWORD" in str(type(e)).upper() or "PASSWORD" in str(e).upper():
            if not twofa:
                twofa = getpass("This account requires 2FA. Enter password: ")
            await client.sign_in(password=twofa)
        else:
            raise

def guess_owner_id_from_gift(unique):
    """
    Try to extract a user id / peer that represents the current owner/seller.
    We probe common fields Telegram uses in TL objects.
    """
    # direct scalar fields
    for fld in ("owner_id", "seller_id", "user_id", "from_id"):
        v = getattr(unique, fld, None)
        if isinstance(v, int):
            return v
        # TL peer variants
        try:
            from telethon.tl import types as _t
            if isinstance(v, _t.PeerUser):
                return v.user_id
            if isinstance(v, _t.InputPeerUser):
                return v.user_id
            if isinstance(v, _t.User):
                return v.id
        except Exception:
            pass

    # nested 'owner' object (if present)
    owner = getattr(unique, "owner", None)
    if owner is not None:
        return getattr(owner, "user_id", getattr(owner, "id", None))

    return None


async def try_get_owner_profile(client, unique):
    """
    Attempt to resolve the owner profile into an entity.
    Return the entity on success, None if not resolvable.
    """
    uid = guess_owner_id_from_gift(unique)
    if not uid:
        return None
    try:
        return await client.get_entity(uid)
    except Exception:
        return None


def has_message_metadata(unique) -> bool:
    """
    Your existing heuristic (left intact): scan attributes for message/greeting/note.
    """
    try:
        attrs = getattr(unique, "attributes", None) or []
        for a in attrs:
            cls = a.__class__.__name__.lower()
            if any(k in cls for k in ("message", "greeting", "note", "sender", "recipient")):
                return True
            if any(hasattr(a, fld) for fld in (
                "message", "text", "sender", "recipient",
                "from_id", "to_id", "sender_name", "recipient_name"
            )):
                return True
    except Exception:
        pass
    return False


def detect_msgmeta_status(unique, owner_peer) -> str:
    """
    Returns 'yes' | 'no' | 'probably'
    - 'yes' if attributes clearly carry message metadata.
    - 'no'  if no metadata AND we could resolve owner profile.
    - 'probably' if no metadata but owner profile could not be found.
    """
    if has_message_metadata(unique):
        return "yes"
    return "probably" if owner_peer is None else "no"


def message_meta_allows_status(status: str, policy: str) -> bool:
    """
    policy: 'any' | 'require' | 'exclude'
    status: 'yes' | 'no' | 'probably'
    - 'require' => only allow when 'yes'
    - 'exclude' => only allow when 'no'
    - 'any'     => allow for all statuses (including 'probably')
    """
    if policy == "any":
        return True
    if policy == "require":
        return status == "yes"
    if policy == "exclude":
        return status == "no"
    return True

def stars_amount_to_int(obj) -> Optional[int]:
    if obj is None: return None
    try:
        amt = getattr(obj, "amount", None)
        return int(amt) if amt is not None else None
    except Exception:
        return None

def ton_amount_to_float(obj) -> Optional[float]:
    if obj is None: return None
    try:
        amt = getattr(obj, "amount", None)
        if amt is None: return None
        return float(amt) / 1_000_000_000.0  # nanoTON -> TON
    except Exception:
        return None

def extract_prices(it) -> Tuple[Optional[int], Optional[float]]:
    stars_price = None
    ton_price = None
    ra = getattr(it, "resell_amount", None)
    if isinstance(ra, list) and ra:
        for x in ra:
            cname = x.__class__.__name__.lower()
            if "starsamount" in cname:
                stars_price = stars_amount_to_int(x)
            elif "starstonamount" in cname or "tonamount" in cname or "ton" in cname:
                ton_price = ton_amount_to_float(x)
        if stars_price is None and len(ra) >= 1:
            stars_price = stars_amount_to_int(ra[0])
        if ton_price is None and len(ra) >= 2:
            ton_price = ton_amount_to_float(ra[1])
    if stars_price is None:
        stars_price = (stars_amount_to_int(getattr(it, "stars_amount", None))
                       or stars_amount_to_int(getattr(it, "price_stars", None)))
    return stars_price, ton_price

# ----------------------- prompts & config -----------------------

@dataclass
class FilterConfig:
    collections: List[str]  # compare with base slug from link
    models: List[str]       # starGiftAttributeModel.name
    backdrops: List[str]    # starGiftAttributeBackdrop.name
    symbols: List[str]      # starGiftAttributePattern.name

@dataclass
class ActionConfig:
    mode: str               # 'print' or 'buy'
    dest: Optional[str]     # for print: where to send (None => console)
    buyer_recipient: Optional[str]  # for buy: @username/id to receive the gift
    ton: Optional[bool]
    max_ton: Optional[float]
    min_profit_ton: Optional[float]  # NEW: minimum profit in TON (None = no filter)

@dataclass
class FilterConfig:
    collections: List[str]
    models: List[str]
    backdrops: List[str]
    symbols: List[str]
    msg_meta_policy: str  # 'any' | 'require' | 'exclude'

def is_float(s):
    try:
        float(s)
        return True
    except ValueError:
        return False

async def prompt_user_prefs() -> Tuple[FilterConfig, ActionConfig]:
    print("\n=== Настройка фильтров (оставьте пустым для пропуска фильтра, разделение запятой для добавления нескольких аттрибутов) ===")
    collections = _csv_list(input("📃 Разрешенные коллекции (пример: LunarSnake,StellarRocket): ").strip())
    models      = _csv_list(input("💼 Разрешенные модели: (пример: Sugar Daddy,Mission Uranus): ").strip())
    backdrops   = _csv_list(input("🎨 Разрешенные фоны: (пример: Black,Ivory White): ").strip())
    symbols     = _csv_list(input("💮 Разрешенные символы: (пример: Egg,Chili): ").strip())

    msg_meta_policy = ""
    while msg_meta_policy not in ("any", "require", "exclude"):
        msg_meta_policy = (input("\n⚠️ Настройка фильтра по надписям: \n"
                                 "🎯 Напишите 'require' чтобы покупать ТОЛЬКО подарки с надписями, \n"
                                 "❌ Напишите 'exclude' чтобы покупать ТОЛЬКО подарки без надписи, \n"
                                 "😌 Или напишите 'any' чтобы не фильтровать по надписям: ")
                           .strip().lower())
    msg_meta_policy = msg_meta_policy or "any"

    mode = ""
    while mode not in ("buy", "print"):
        mode = input("\n💵 Покупать ли? \nНапишите 'buy' для включения авто-скупщика, или 'print' для отправки сообщений без покупки: ").strip().lower() or "print"

    dest: Optional[str] = None
    buyer_recipient: Optional[str] = None
    ton: Optional[bool] = True
    dest_in = input("\n🕵️‍♀️ Где отписывать? \n(оставьте пустым чтобы писать в консоль, или впишите @username / chat_id): ").strip()
    dest = dest_in or None
    max_ton = 100000

    buyer_recipient = input("\n🎁 Покупать подарок кому-то? \nЕсли да, введите @username или user_id: ").strip()
    if not buyer_recipient:
        print("⚠️ Вы не выбрали получателя подарка! По умолчанию подарок будет отправляться вам-же.")
        tont = ''
        while tont not in ("да", "нет"):
            tont = input("\n💎️ Покупать за TON? (да/нет): ").strip()
        ton = True if tont == "да" else False
        max_ton = 'p'
        while not is_float(max_ton) and max_ton != '':
            max_ton = input("\n🛡️ Максимальная цена в TON? (оставьте пустым чтобы убрать лимит): ").strip()
        if max_ton == '':
            max_ton = 100000
        max_ton = float(max_ton)
        buyer_recipient = 'no'

    # NEW: ask for min profit (TON)
    min_profit_ton: Optional[float] = None
    _mp = input("\n📈 Минимальная окупаемость в TON? (например 0.05; пусто = без фильтра): ").strip().replace(',', '.')
    if _mp:
        try:
            min_profit_ton = float(_mp)
        except ValueError:
            print("⚠️ Некорректное значение, фильтр по окупаемости выключен.")
            min_profit_ton = None

    print('\n')
    return (
        FilterConfig(collections, models, backdrops, symbols, msg_meta_policy),
        ActionConfig(mode, dest, buyer_recipient, ton, max_ton, min_profit_ton)
    )

async def resolve_peer(client: TelegramClient, s: str):
    """Accepts @username or numeric id; returns InputPeer via get_input_entity."""
    if not s:
        raise ValueError("Empty peer")
    try:
        if s.startswith("@"):
            return await client.get_input_entity(s)
        # numeric?
        if s.lstrip("-").isdigit():
            return await client.get_input_entity(int(s))
        return await client.get_input_entity(s)
    except Exception as e:
        raise RuntimeError(f"Could not resolve peer '{s}': {e}")

# ----------------------- gift API -----------------------

def extract_titles_from_attributes(unique: types.StarGift) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (model_title, backdrop_title, symbol_title)
    - model   : starGiftAttributeModel.name
    - backdrop: starGiftAttributeBackdrop.name
    - symbol  : starGiftAttributePattern.name (repeating icon on the backdrop)
    """
    model = backdrop = symbol = None
    attrs = getattr(unique, "attributes", None) or []
    for a in attrs:
        cls = a.__class__.__name__.lower()
        name = getattr(a, "name", None) or None
        if not name:
            continue
        if "model" in cls and model is None:
            model = name
        elif "backdrop" in cls and backdrop is None:
            backdrop = name
        elif "pattern" in cls and symbol is None:
            symbol = name
    return model, backdrop, symbol

def matches_filters(base_slug: str,
                    model: Optional[str],
                    backdrop: Optional[str],
                    symbol: Optional[str],
                    f: FilterConfig) -> bool:
    if f.collections and base_slug.lower() not in f.collections and not _contains_any(base_slug, f.collections):
        return False
    if f.models and not _contains_any(model, f.models):
        return False
    if f.backdrops and not _contains_any(backdrop, f.backdrops):
        return False
    if f.symbols and not _contains_any(symbol, f.symbols):
        return False
    return True

async def fetch_unique_by_link(client: TelegramClient, link: str) -> Optional[types.StarGift]:
    m = LINK_RE.search(link)
    if not m:
        return None
    full_slug = m.group(1)
    slug = full_slug.lower()
    try:
        res = await client(functions.payments.GetUniqueStarGiftRequest(slug=slug))
    except Exception as e:
        print(f"[!] {full_slug}: getUniqueStarGift failed: {e}")
        return None
    return getattr(res, "gift", None)

async def maybe_print_or_buy(client: TelegramClient,
                             link: str,
                             unique: types.StarGift,
                             f: FilterConfig,
                             a: ActionConfig,
                             profit_ton: Optional[float] = None):   # NEW
    global done
    # Base slug for collection filter
    m = LINK_RE.search(link)
    full_slug = m.group(1) if m else ""
    base_slug, _ = split_slug_to_base_and_num(full_slug)

    # Titles
    model, backdrop, symbol = extract_titles_from_attributes(unique)
    stars_price, ton_price = extract_prices(unique)
    print(stars_price, ton_price)

    if ton_price and ton_price > a.max_ton:
        return

    # Attribute-name filters
    def _contains_any(haystack: Optional[str], needles: List[str]) -> bool:
        if not needles:
            return True
        h = (haystack or "").lower()
        return any(n in h for n in needles)

    if f.collections and base_slug.lower() not in f.collections and not _contains_any(base_slug, f.collections):
        return
    if f.models and not _contains_any(model, f.models):
        return
    if f.backdrops and not _contains_any(backdrop, f.backdrops):
        return
    if f.symbols and not _contains_any(symbol, f.symbols):
        return

    # Message-metadata status
    owner_peer = await try_get_owner_profile(client, unique)
    msg_status = detect_msgmeta_status(unique, owner_peer)  # 'yes'|'no'|'probably'

    # Enforce policy at filtering step
    if not message_meta_allows_status(msg_status, f.msg_meta_policy):
        return

        # --- Profit filter (TON) ---
    if a.min_profit_ton is not None:
        if profit_ton is None:
            # No profit parsed from the feed; safest is to skip.
            # If you prefer to allow in this case, comment-out the next line.
            return
        if profit_ton < a.min_profit_ton:
            return

    # Print line (now includes 'probably')
    line = (f"Подарок найден! {full_slug} → \n"
            f"Модель: {model or '—'} | Фон: {backdrop or '—'} | Символ: {symbol or '—'} | "
            f"Надпись: {msg_status} | "
            f"Окупаемость: {profit_ton if profit_ton is not None else '—'} TON")
    print(line)

    # PRINT mode
    if a.dest:
        try:
            peer = await resolve_peer(client, a.dest)
            await client.send_message(peer, f"{line}\n{link}")
        except Exception as e:
            print(f"[!] Не могу отправить сообщение {a.dest}: {e}")

    if a.mode == "print":
        return

    # BUY mode
    # Special rule you asked for:
    # - If msgdata is 'probably', we ONLY buy when policy is 'any'.
    if msg_status == "probably" and f.msg_meta_policy != "any":
        print(f"[skip-buy] {full_slug}: MsgMeta = 'probably', а политика равна '{f.msg_meta_policy}'.")
        return

    if not a.buyer_recipient:
        print("[!] Не найден получаль подарка, пропускаем")
        return

    try:
        if a.buyer_recipient != 'no':
            to_peer = await resolve_peer(client, a.buyer_recipient)
        else:
            to_peer = types.InputPeerSelf()
    except Exception as e:
        print(f"[!] Не могу получить обьект получателя подарка '{a.buyer_recipient}': {e}")
        return

    try:
        if not done:
            # Stars flow (as before)
            invoice = types.InputInvoiceStarGiftResale(slug=full_slug.lower(), to_id=to_peer, ton=a.ton)
            pay_form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
            result = await client(functions.payments.SendStarsFormRequest(form_id=pay_form.form_id, invoice=invoice))
            print(f"[BUY] Результат покупки: {type(result).__name__}")
            if buy_one:
                done = True
    except Exception as e:
        print(f"[!] Автопокупка провалена для {full_slug}: {e}")

# ----------------------- main -----------------------

async def main():
    # API creds + session
    api_id = 20170763
    api_hash = '677b3c7bedb8facc947ed708be29a235'
    session_str = read_session_string()
    session = StringSession(session_str) if session_str else StringSession()
    client = TelegramClient(session, int(api_id), api_hash)

    if not session_str:
        await client.start(
            phone=input("Номер телефона: "),
            password=input("2FA пароль (оставьте пустым если отсуствует): "),
        )

    async with client:
        await ensure_logged_in(client)
        # Save session
        new_s = client.session.save()
        if new_s and new_s != session_str:
            write_session_string(new_s)

        # Ask user preferences once at startup
        filters, action = await prompt_user_prefs()

        # Resolve feed
        channel = await client.get_entity(CHANNEL)

        print("✅ Запущено! Поиск подарков производится через пир t.me/giftmarketfeed...")

        # Windows-safe stdin listener
        async def _stdin_listener():
            loop = asyncio.get_running_loop()
            while True:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                ln = line.strip()
                if not ln:
                    continue
                unique = await fetch_unique_by_link(client, ln)
                if unique:
                    await maybe_print_or_buy(client, ln, unique, filters, action, profit_ton=None)

        asyncio.create_task(_stdin_listener())

        @client.on(events.NewMessage(chats=channel))
        async def on_new_message(event: events.NewMessage.Event):
            text = event.message.message or ""
            links = [f"https://t.me/nft/{slug}" for slug in LINK_RE.findall(text)]

            # Buttons too
            if event.message.buttons:
                for row in event.message.buttons:
                    for btn in row:
                        url = getattr(btn, "url", None)
                        if url and LINK_RE.search(url):
                            links.append(url)

            # Parse profits in order; if count matches links, map 1:1; else use first for all
            raw_profits = PROFIT_RE.findall(text)
            profits: List[Optional[float]] = [None] * len(links)
            parsed = [float(x.replace(",", ".")) for x in raw_profits] if raw_profits else []
            if parsed:
                if len(parsed) == len(links):
                    profits = parsed
                else:
                    profits = [parsed[0]] * len(links)

            # Dedup by link, keep corresponding profit for first occurrence
            seen = {}
            for i, link in enumerate(links):
                if link in seen:
                    continue
                seen[link] = True
                unique = await fetch_unique_by_link(client, link)
                if unique:
                    profit_ton = profits[i] if i < len(profits) else None
                    await maybe_print_or_buy(client, link, unique, filters, action, profit_ton=profit_ton)

        print("[*] Ищем подарки через пир @giftmarketfeed Ctrl+C для завершения.")
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bye] Остановлено.")
