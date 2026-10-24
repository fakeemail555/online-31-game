import asyncio
import random
import string
from dataclasses import dataclass, field
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="31 Online")
app.mount("/static", StaticFiles(directory="static"), name="static")

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
VALUES = {"A": 11, "J": 10, "Q": 10, "K": 10}
STARTING_CHIPS = 100
ANTE = 5
MAX_PLAYERS = 3

def card_value(rank):
    return VALUES.get(rank, int(rank) if rank.isdigit() else 10)

def card_label(card):
    return f'{card["rank"]}{card["suit"]}'

def hand_score(cards):
    # Score only the strongest suit, as described in the user's rules.
    totals = {}
    for c in cards:
        totals[c["suit"]] = totals.get(c["suit"], 0) + card_value(c["rank"])
    return max(totals.values()) if totals else 0

def make_deck():
    deck = [{"rank": r, "suit": s} for s in SUITS for r in RANKS]
    random.SystemRandom().shuffle(deck)
    return deck

@dataclass
class Player:
    pid: str
    name: str
    ws: WebSocket
    chips: int = STARTING_CHIPS
    hand: list = field(default_factory=list)
    folded: bool = False
    connected: bool = True
    street_bet: int = 0
    total_bet: int = 0
    acted: bool = False
    draws_used: int = 0

@dataclass
class Room:
    code: str
    players: list = field(default_factory=list)
    deck: list = field(default_factory=list)
    pot: int = 0
    dealer_index: int = 0
    phase: str = "lobby"
    current_index: int = 0
    current_bet: int = 0
    min_raise: int = 5
    last_message: str = "در انتظار بازیکنان..."
    hand_number: int = 0
    draw_queue: list = field(default_factory=list)
    draw_turn: int = 0
    round_actions: int = 0
    winner_id: Optional[str] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

rooms: dict[str, Room] = {}

def next_active_index(room, start, require_not_folded=True):
    n = len(room.players)
    for offset in range(1, n + 1):
        i = (start + offset) % n
        p = room.players[i]
        if p.connected and (not require_not_folded or not p.folded) and p.chips >= 0:
            return i
    return start

def active_players(room):
    return [p for p in room.players if not p.folded and p.connected]

def room_status(room, viewer_id):
    viewer = next((p for p in room.players if p.pid == viewer_id), None)
    public_players = []
    for p in room.players:
        public_players.append({
            "id": p.pid, "name": p.name, "chips": p.chips,
            "folded": p.folded, "connected": p.connected,
            "street_bet": p.street_bet,
            "is_dealer": room.players.index(p) == room.dealer_index,
            "is_turn": room.phase in ("betting", "drawing") and room.players[room.current_index].pid == p.pid,
            "cards": [card_label(c) for c in p.hand] if p.pid == viewer_id or room.phase in ("showdown", "finished") else [],
            "card_count": len(p.hand),
            "score": hand_score(p.hand) if room.phase in ("showdown", "finished") and not p.folded else None,
        })
    return {
        "type": "state", "code": room.code, "phase": room.phase,
        "players": public_players, "pot": room.pot, "ante": ANTE,
        "current_bet": room.current_bet, "min_raise": room.min_raise,
        "current_player_id": room.players[room.current_index].pid if room.players and room.phase in ("betting", "drawing") else None,
        "your_id": viewer_id, "your_chips": viewer.chips if viewer else 0,
        "your_cards": [card_label(c) for c in viewer.hand] if viewer else [],
        "draws_used": viewer.draws_used if viewer else 0,
        "last_message": room.last_message, "hand_number": room.hand_number,
        "winner_id": room.winner_id,
    }

async def broadcast(room):
    for p in list(room.players):
        if p.connected:
            try:
                await p.ws.send_json(room_status(room, p.pid))
            except Exception:
                p.connected = False

async def send_error(ws, message):
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass

def reset_betting(room):
    room.current_bet = 0
    room.min_raise = 5
    for p in room.players:
        p.street_bet = 0
        p.acted = False

def begin_hand(room):
    room.hand_number += 1
    room.deck = make_deck()
    room.pot = 0
    room.winner_id = None
    room.last_message = f"دست شماره {room.hand_number} شروع شد."
    for p in room.players:
        p.hand = []
        p.folded = False
        p.street_bet = 0
        p.total_bet = 0
        p.acted = False
        p.draws_used = 0
    # Ante
    for p in room.players:
        paid = min(ANTE, p.chips)
        p.chips -= paid
        p.total_bet += paid
        room.pot += paid
    for _ in range(3):
        for p in room.players:
            p.hand.append(room.deck.pop())
    room.dealer_index = room.dealer_index % len(room.players)
    room.current_index = (room.dealer_index + 1) % len(room.players)
    room.phase = "betting"
    room.round_actions = 0

def betting_action(room, player, action, amount=0):
    live = active_players(room)
    if len(live) <= 1:
        room.phase = "finished"
        room.winner_id = live[0].pid if live else None
        if live:
            live[0].chips += room.pot
        room.last_message = f"{live[0].name} با فولد دیگران برنده شد." if live else "دست پایان یافت."
        room.pot = 0
        return
    to_call = max(0, room.current_bet - player.street_bet)
    action = action.lower()
    if action == "fold":
        player.folded = True
        player.acted = True
        room.last_message = f"{player.name} فولد کرد."
    elif action == "check":
        if to_call:
            raise ValueError("وقتی شرطی برای پرداخت وجود دارد، نمی‌توان چک کرد؛ کال یا فولد کن.")
        player.acted = True
        room.last_message = f"{player.name} چک کرد."
    elif action == "call":
        if to_call == 0:
            player.acted = True
            room.last_message = f"{player.name} چک کرد."
        else:
            paid = min(to_call, player.chips)
            player.chips -= paid
            player.street_bet += paid
            player.total_bet += paid
            room.pot += paid
            player.acted = True
            room.last_message = f"{player.name} کال کرد."
    elif action in ("bet", "raise"):
        target = int(amount)
        if target <= 0:
            raise ValueError("مبلغ شرط باید بیشتر از صفر باشد.")
        # amount is the total street contribution the player wants after this action.
        if target <= room.current_bet:
            raise ValueError("برای بت/ریز، مبلغ نهایی شرط باید از شرط فعلی بیشتر باشد.")
        raise_by = target - room.current_bet
        if room.current_bet and raise_by < room.min_raise:
            raise ValueError(f"حداقل افزایش شرط {room.min_raise} چیپ است.")
        needed = target - player.street_bet
        if needed > player.chips:
            raise ValueError("چیپ کافی نداری.")
        player.chips -= needed
        room.pot += needed
        player.total_bet += needed
        player.street_bet = target
        room.min_raise = max(5, raise_by)
        room.current_bet = target
        for p in room.players:
            if p.pid != player.pid and not p.folded:
                p.acted = False
        player.acted = True
        room.last_message = f"{player.name} شرط را به {target} چیپ رساند."
    else:
        raise ValueError("عملیات شرط‌بندی نامعتبر است.")

    live = active_players(room)
    if len(live) <= 1:
        winner = live[0] if live else None
        if winner:
            winner.chips += room.pot
            room.winner_id = winner.pid
        room.pot = 0
        room.phase = "finished"
        room.last_message = f"{winner.name} برنده شد؛ همه بازیکنان دیگر فولد کردند." if winner else "دست پایان یافت."
        return

    # Betting round ends when every live player has acted and matched current bet.
    if all(p.acted and (p.street_bet == room.current_bet or p.chips == 0) for p in live):
        room.phase = "drawing"
        room.draw_queue = [(room.dealer_index + i) % len(room.players) for i in range(1, len(room.players) + 1)]
        room.draw_queue = [i for i in room.draw_queue if not room.players[i].folded]
        room.draw_turn = 0
        room.current_index = room.draw_queue[0] if room.draw_queue else room.dealer_index
        room.last_message = "شرط‌بندی تمام شد؛ مرحله تعویض کارت شروع می‌شود."
    else:
        room.current_index = next_active_index(room, room.current_index)

def start_next_hand(room):
    room.dealer_index = (room.dealer_index + 1) % len(room.players)
    begin_hand(room)

@app.get("/")
async def index():
    return FileResponse("static/index.html")

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    player = None
    room = None
    try:
        first = await ws.receive_json()
        if first.get("action") != "join":
            await send_error(ws, "ابتدا باید به اتاق وارد شوی.")
            await ws.close()
            return
        name = str(first.get("name", "بازیکن")).strip()[:20] or "بازیکن"
        code = str(first.get("code", "")).strip().upper()
        async with asyncio.Lock():
            if not code or code == "NEW":
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
                while code in rooms:
                    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
                room = Room(code=code)
                rooms[code] = room
            else:
                room = rooms.get(code)
            if room is None:
                await send_error(ws, "این اتاق پیدا نشد.")
                await ws.close()
                return
        async with room.lock:
            if room.phase != "lobby":
                await send_error(ws, "این دست شروع شده؛ در نسخه اولیه ورود وسط دست مجاز نیست.")
                await ws.close()
                return
            if len(room.players) >= MAX_PLAYERS:
                await send_error(ws, "ظرفیت این اتاق تکمیل است.")
                await ws.close()
                return
            player = Player(pid=''.join(random.choices(string.ascii_lowercase + string.digits, k=8)), name=name, ws=ws)
            room.players.append(player)
            room.last_message = f"{name} وارد اتاق شد ({len(room.players)}/3)."
            await broadcast(room)

        while True:
            msg = await ws.receive_json()
            async with room.lock:
                if not player:
                    continue
                action = msg.get("action")
                try:
                    if action == "start":
                        if len(room.players) != 3:
                            raise ValueError("برای شروع، هر سه بازیکن باید وارد اتاق شده باشند.")
                        if room.phase not in ("lobby", "finished"):
                            raise ValueError("یک دست در حال اجراست.")
                        if any(p.chips < ANTE for p in room.players):
                            raise ValueError("یکی از بازیکنان چیپ کافی برای ورودی ندارد.")
                        begin_hand(room)
                    elif action in ("check", "call", "bet", "raise", "fold"):
                        if room.phase != "betting":
                            raise ValueError("الان مرحله شرط‌بندی نیست.")
                        if room.players[room.current_index].pid != player.pid:
                            raise ValueError("الان نوبت تو نیست.")
                        betting_action(room, player, action, msg.get("amount", 0))
                    elif action == "draw":
                        if room.phase != "drawing":
                            raise ValueError("الان مرحله تعویض کارت نیست.")
                        if room.players[room.current_index].pid != player.pid:
                            raise ValueError("الان نوبت تو نیست.")
                        if player.draws_used >= 2:
                            raise ValueError("هر بازیکن حداکثر دو بار می‌تواند تعویض کند.")
                        indices = msg.get("indices", [])
                        if not isinstance(indices, list) or len(indices) > 2 or any(not isinstance(i, int) or i not in (0,1,2) for i in indices) or len(set(indices)) != len(indices):
                            raise ValueError("کارت‌های انتخابی نامعتبرند.")
                        for idx in sorted(indices, reverse=True):
                            player.hand.pop(idx)
                        for _ in indices:
                            player.hand.append(room.deck.pop())
                        player.draws_used += 1
                        room.last_message = f"{player.name} تعویض کارت انجام داد ({player.draws_used}/2)."
                        # 31 ends immediately.
                        if len(player.hand) == 3 and hand_score(player.hand) == 31 and len({c["suit"] for c in player.hand}) == 1:
                            player.chips += room.pot
                            room.winner_id = player.pid
                            room.pot = 0
                            room.phase = "finished"
                            room.last_message = f"بونس ۳۱! {player.name} فوراً برنده این دست شد."
                        else:
                            room.draw_turn += 1
                            if room.draw_turn >= len(room.draw_queue):
                                room.phase = "showdown"
                                scores = {p.pid: hand_score(p.hand) for p in room.players if not p.folded}
                                best = max(scores.values()) if scores else 0
                                winners = [p for p in room.players if not p.folded and scores.get(p.pid) == best]
                                if winners:
                                    share, rem = divmod(room.pot, len(winners))
                                    for i, w in enumerate(winners):
                                        w.chips += share + (1 if i < rem else 0)
                                    room.winner_id = winners[0].pid if len(winners) == 1 else None
                                    room.last_message = ("برنده: " + ", ".join(w.name for w in winners) + f" با امتیاز {best}.")
                                room.pot = 0
                            else:
                                room.current_index = room.draw_queue[room.draw_turn]
                    elif action == "stand":
                        if room.phase != "drawing":
                            raise ValueError("الان مرحله تعویض کارت نیست.")
                        if room.players[room.current_index].pid != player.pid:
                            raise ValueError("الان نوبت تو نیست.")
                        player.draws_used = 2
                        room.last_message = f"{player.name} تعویض را تمام کرد."
                        room.draw_turn += 1
                        if room.draw_turn >= len(room.draw_queue):
                            room.phase = "showdown"
                            scores = {p.pid: hand_score(p.hand) for p in room.players if not p.folded}
                            best = max(scores.values()) if scores else 0
                            winners = [p for p in room.players if not p.folded and scores.get(p.pid) == best]
                            if winners:
                                share, rem = divmod(room.pot, len(winners))
                                for i, w in enumerate(winners):
                                    w.chips += share + (1 if i < rem else 0)
                                room.winner_id = winners[0].pid if len(winners) == 1 else None
                                room.last_message = "برنده: " + ", ".join(w.name for w in winners) + f" با امتیاز {best}."
                            room.pot = 0
                        else:
                            room.current_index = room.draw_queue[room.draw_turn]
                    elif action == "next":
                        if room.phase not in ("finished", "showdown"):
                            raise ValueError("دست فعلی هنوز تمام نشده است.")
                        if len(room.players) != 3:
                            raise ValueError("هر سه بازیکن باید متصل باشند.")
                        start_next_hand(room)
                    else:
                        raise ValueError("عملیات ناشناخته است.")
                except (ValueError, TypeError) as e:
                    await send_error(ws, str(e))
                await broadcast(room)
    except WebSocketDisconnect:
        if room and player:
            async with room.lock:
                player.connected = False
                room.last_message = f"{player.name} قطع شد."
                await broadcast(room)
    except Exception:
        if room and player:
            async with room.lock:
                player.connected = False
                room.last_message = f"{player.name} اتصالش قطع شد."
                await broadcast(room)
