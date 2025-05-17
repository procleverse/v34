import logging
import random
import re
import requests
from datetime import datetime, timedelta
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    JobQueue
)
from typing import Dict, List, Tuple, Optional
from enum import Enum, auto

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "7150323529:AAH5eVyWGZeANIHK58pH6aVuZpOedLtFD4A"
DATABASE_PATH = "database.db"
ADMIN_IDS = [7765875067, 5042988950]
MAX_PLAYERS = 6
MIN_BET = 50
MAX_BET = 10000
DEFAULT_BALANCE = 0
DEALER_FEE_PERCENT = 20  # 20% комиссия дилеру
FAST_MODE_BET = 100  # Фиксированная ставка для быстрого режима
MIN_WITHDRAW = 1000  # Минимальная сумма вывода

db_connection: Optional[aiosqlite.Connection] = None

# Crypto Bot configuration
CRYPTO_BOT_API_KEY = "362437:AA54iCl9i8kGn1YGtWhsIAIZEpDCeOpOYYu"
CRYPTO_BOT_USERNAME = "SekaPlaybot"
CRYPTO_BOT_API_URL = "https://pay.crypt.bot/api/"

EMOJI = {
    'hearts': '♥️',
    'diamonds': '♦️',
    'clubs': '♣️',
    'spades': '♠️',
    'money': '💰',
    'cards': '🃏',
    'trophy': '🏆',
    'fire': '🔥',
    'skull': '💀',
    'clock': '⏳',
    'check': '✅',
    'cross': '❌',
    'home': '🏠',
    'profile': '👤',
    'balance': '💰',
    'top': '🏆',
    'info': 'ℹ️',
    'add': '➕',
    'deposit': '📥',
    'withdraw': '📤',
    'help': '❓',
    'game': '🎮',
    'dice': '🎲',
    'warning': '⚠️',
    'bank': '🏦',
    'sword': '⚔️',
    'handshake': '🤝'
}

async def get_exchange_rate(from_currency: str, to_currency: str) -> float:
    """
    Возвращает количество USDT за 1 RUB (или наоборот), используя актуальный курс.
    RUB -> USDT: возвращает сколько USDT за 1 RUB (пример: 0.011)
    """
    if from_currency == "RUB" and to_currency == "USDT":
        try:
            # Получаем курс RUB к USDT через Binance API (актуальный)
            response = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB", timeout=10)
            data = response.json()
            price = float(data["price"])  # RUB за 1 USDT
            return 1 / price  # USDT за 1 RUB
        except Exception as e:
            logger.error("Ошибка получения курса RUB/USDT: %s", e)
            return 1 / 90.0  # fallback
    elif from_currency == "USDT" and to_currency == "RUB":
        try:
            response = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTRUB", timeout=10)
            data = response.json()
            price = float(data["price"])
            return price  # RUB за 1 USDT
        except Exception as e:
            logger.error("Ошибка получения курса USDT/RUB: %s", e)
            return 90.0
    return 1.0

class GameState(Enum):
    WAITING = auto()
    JOINING = auto()
    BIDDING = auto()
    AWAITING_CONFIRMATION = auto()
    FINAL_CHOICE = auto()
    FINAL_SWARA_WAIT = auto()
    SHOWDOWN = auto()
    FINISHED = auto()

class GameMode(Enum):
    NORMAL = auto()
    FAST = auto()
    TOURNAMENT = auto()

class PlayerAction(Enum):
    FOLD = auto()
    RAISE = auto()
    CALL = auto()
    CHECK = auto()
    SHOW = auto()
    LOOK = auto()
    SWARA = auto()

class Card:
    def __init__(self, rank: str, suit: str):
        self.rank = rank
        self.suit = suit
        self.value = self._calculate_value()
    
    def _calculate_value(self) -> int:
        rank_values = {
            '6': 6, '7': 7, '8': 8, '9': 9,
            '10': 10, 'J': 10, 'Q': 10, 'K': 10,
            'A': 11
        }
        return rank_values.get(self.rank, 0)
    
    def __str__(self) -> str:
        if self.rank == '7' and self.suit == 'clubs':  # Joker
            return "JOK"
        suit_emoji = EMOJI.get(self.suit, self.suit)
        return f"{self.rank}{suit_emoji}"
    
    def __repr__(self) -> str:
        return self.__str__()
    
    def __eq__(self, other):
        return self.rank == other.rank and self.suit == other.suit

class SekaGame:
    def __init__(self, chat_id: int, creator_id: int, bet_amount: int, game_mode: GameMode = GameMode.NORMAL):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.bet_amount = bet_amount
        self.players: Dict[int, dict] = {}
        self.state = GameState.WAITING
        self.current_player = None
        self.deck: List[Card] = []
        self.pot = 0
        self.max_bid = 0
        self.last_raiser = None
        self.game_mode = game_mode
        self.initialize_deck()
        self.show_confirmations = set()
        self.initial_player_count = 0
        self.final_split_set = set()
        self.final_swara_set = set()
        self.start_time = datetime.now()
        self.timeout_job = None
        self.swara_participants = set()
        self.swara_cost = 0
        self.swara_message_id = None
        self.final_choice_set = set()

    def initialize_deck(self) -> None:
        suits = ['hearts', 'diamonds', 'clubs', 'spades']
        ranks = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
        self.deck = [Card(rank, suit) for suit in suits for rank in ranks]
    
    def shuffle_deck(self) -> None:
        random.shuffle(self.deck)
    
    async def add_player(self, user_id: int, user_name: str) -> bool:
        if user_id in self.players:
            return False
            
        if len(self.players) >= MAX_PLAYERS:
            return False
            
        balance = await self.get_user_balance(user_id)
        if balance < self.bet_amount:
            return False
            
        self.players[user_id] = {
            'name': user_name,
            'cards': [],
            'balance': balance,
            'mode': GameMode.NORMAL,
            'bid': 0,
            'folded': False,
            'shown': False,
            'message_id': None,
            'joined_time': datetime.now()
        }
        return True
    
    async def start_game(self) -> bool:
        if len(self.players) < 2:
            return False
            
        for user_id in self.players:
            balance = await self.get_user_balance(user_id)
            if balance < self.bet_amount:
                return False
            self.players[user_id]['balance'] = balance
            
        for user_id in self.players:
            await self.update_user_balance(user_id, -self.bet_amount, "game_bet")
            self.players[user_id]['balance'] -= self.bet_amount
            
        self.shuffle_deck()
        self.deal_cards()
        
        if self.game_mode == GameMode.FAST:
            self.state = GameState.SHOWDOWN
        else:
            self.state = GameState.BIDDING
            self.current_player = next(iter(self.players))
            
        self.pot = len(self.players) * self.bet_amount
        self.max_bid = self.bet_amount
        
        for player in self.players.values():
            player['bid'] = self.bet_amount
            
        self.initial_player_count = len(self.players)
        return True

    def deal_cards(self) -> None:
        cards_per_player = 3
        for player in self.players.values():
            if not player.get('folded', False):
                player['cards'] = [self.deck.pop() for _ in range(cards_per_player)]
    
    def calculate_hand_value(self, cards: List[Card]) -> int:
        if all(card.rank == '6' for card in cards):
            return 34
            
        joker_present = any(str(card) == "JOK" for card in cards)
        non_joker = [card for card in cards if str(card) != "JOK"]
        
        if joker_present and len(non_joker) == 2 and all(card.rank == '6' for card in non_joker):
            return 34
            
        ace_count = sum(1 for card in cards if card.rank == 'A')
        if ace_count >= 2:
            return 22
            
        if len(set(card.rank for card in cards)) == 1:
            return sum(card.value for card in cards)
            
        suit_totals = {}
        for card in cards:
            if str(card) == "JOK":  # Joker adds to the highest suit
                continue
            card_val = card.value
            suit_totals[card.suit] = suit_totals.get(card.suit, 0) + card_val
        
        # Add joker to the highest suit if present
        if joker_present:
            if suit_totals:
                max_suit = max(suit_totals, key=suit_totals.get)
                suit_totals[max_suit] += 11  # Joker value is 11
            else:
                # If only joker is present (shouldn't happen with 3 cards)
                return 11
        
        return max(suit_totals.values()) if suit_totals else 0

    async def determine_winner(self, context: ContextTypes.DEFAULT_TYPE) -> Dict[int, int]:
        if self.state != GameState.SHOWDOWN:
            return {}
            
        active_players = {pid: p for pid, p in self.players.items() if not p['folded']}
        player_scores = {pid: self.calculate_hand_value(p['cards']) for pid, p in active_players.items()}
        max_score = max(player_scores.values(), default=0)
        winners = [pid for pid, score in player_scores.items() if score == max_score]
        
        if len(winners) > 1:
            # Ничья - делим банк между победителями
            win_amount = self.pot // len(winners)
            dealer_fee = int(win_amount * DEALER_FEE_PERCENT / 100)
            player_win_amount = win_amount - dealer_fee
            
            # Зачисляем выигрыш всем победителям
            for winner in winners:
                await self.update_user_balance(winner, player_win_amount, "game_win")
                self.players[winner]['balance'] += player_win_amount
            
            # Зачисляем комиссию дилеру (боту)
            total_dealer_fee = dealer_fee * len(winners)
            await self.update_user_balance(0, total_dealer_fee, "dealer_fee")
            
            # Show all players' cards
            cards_text = []
            for pid, p in self.players.items():
                hand_value = self.calculate_hand_value(p['cards'])
                cards_text.append(f"🃏 {p['name']}: {' '.join(str(c) for c in p['cards'])} (сила: {hand_value})")
            
            # Сообщение в чат игры
            winners_names = ", ".join(self.players[pid]['name'] for pid in winners)
            await context.bot.send_message(
                chat_id=self.chat_id,
                text=(f"🤝 Ничья! {winners_names} делят банк и получают по {player_win_amount} {EMOJI['money']}!\n"
                      f"🏦 Общая комиссия дилера: {total_dealer_fee} {EMOJI['money']}\n\n" +
                      "Карты игроков:\n" + "\n".join(cards_text) +
                      "\n\n🔄 Для новой игры напишите /begin или /fast")
            )
            
            # Отправляем личные сообщения всем участникам
            for pid, player in self.players.items():
                try:
                    if pid in winners:
                        message = (
                            f"🤝 Вы разделили банк с другими игроками и получаете {player_win_amount} {EMOJI['money']}!\n"
                            f"🏦 Комиссия дилера: {dealer_fee} {EMOJI['money']}\n\n"
                            f"Ваши карты: {self.get_player_cards(pid)}\n"
                            f"Сила руки: {self.calculate_hand_value(player['cards'])}\n\n"
                            f"💵 Ваш выигрыш зачислен на баланс."
                        )
                    else:
                        message = (
                            f"😢 К сожалению, вы проиграли в этой игре.\n\n"
                            f"Ваши карты: {self.get_player_cards(pid)}\n"
                            f"Сила руки: {self.calculate_hand_value(player['cards'])}\n\n"
                            f"Победители: {winners_names} с силой руки {max_score}\n"
                            f"Вы потеряли ставку: {self.bet_amount} {EMOJI['money']}"
                        )
                    
                    await context.bot.send_message(
                        chat_id=pid,
                        text=message
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки личного сообщения игроку {pid}: {e}")
            
            self.state = GameState.FINISHED
            
            # Record game results
            for pid in self.players:
                result = 'win' if pid in winners else 'lose'
                profit = player_win_amount if pid in winners else -self.bet_amount
                await db_connection.execute(
                    'INSERT INTO game_history (user_id, game_id, bet_amount, result, profit) VALUES (?, ?, ?, ?, ?)',
                    (pid, self.chat_id, self.bet_amount, result, profit)
                )
            await db_connection.commit()
            
            # Remove game from active games
            if self.chat_id in active_games:
                del active_games[self.chat_id]
            
            return {winner: player_win_amount for winner in winners}
            
        win_amount = self.pot
        winner = winners[0]
        
        # Вычисляем комиссию дилера (20%)
        dealer_fee = int(win_amount * DEALER_FEE_PERCENT / 100)
        player_win_amount = win_amount - dealer_fee
        
        # Зачисляем выигрыш игроку за вычетом комиссии
        await self.update_user_balance(winner, player_win_amount, "game_win")
        self.players[winner]['balance'] += player_win_amount
        
        # Зачисляем комиссию дилеру (боту)
        await self.update_user_balance(0, dealer_fee, "dealer_fee")
        
        # Show all players' cards
        cards_text = []
        for pid, p in self.players.items():
            hand_value = self.calculate_hand_value(p['cards'])
            cards_text.append(f"🃏 {p['name']}: {' '.join(str(c) for c in p['cards'])} (сила: {hand_value})")
        
        # Сообщение в чат игры
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=(f"🎉 {self.players[winner]['name']} выигрывает {player_win_amount} {EMOJI['money']}!\n"
                  f"🏦 Комиссия дилера: {dealer_fee} {EMOJI['money']}\n\n" +
                  "Карты игроков:\n" + "\n".join(cards_text) +
                  "\n\n🔄 Для новой игры напишите /begin или /fast")
        )
        
        # Отправляем личные сообщения всем участникам
        for pid, player in self.players.items():
            try:
                if pid == winner:
                    message = (
                        f"🎉 Поздравляем! Вы выиграли {player_win_amount} {EMOJI['money']}!\n"
                        f"🏦 Комиссия дилера: {dealer_fee} {EMOJI['money']}\n\n"
                        f"Ваши карты: {self.get_player_cards(pid)}\n"
                        f"Сила руки: {self.calculate_hand_value(player['cards'])}\n\n"
                        f"🏆 Ваш выигрыш зачислен на баланс."
                    )
                else:
                    message = (
                        f"😢 К сожалению, вы проиграли в этой игре.\n\n"
                        f"Ваши карты: {self.get_player_cards(pid)}\n"
                        f"Сила руки: {self.calculate_hand_value(player['cards'])}\n\n"
                        f"Победитель: {self.players[winner]['name']} с силой руки {max_score}\n"
                        f"Вы потеряли ставку: {self.bet_amount} {EMOJI['money']}"
                    )
                
                await context.bot.send_message(
                    chat_id=pid,
                    text=message
                )
            except Exception as e:
                logger.error(f"Ошибка отправки личного сообщения игроку {pid}: {e}")
        
        self.state = GameState.FINISHED
        
        # Record game results
        for pid in self.players:
            result = 'win' if pid in winners else 'lose'
            profit = player_win_amount if pid in winners else -self.bet_amount
            await db_connection.execute(
                'INSERT INTO game_history (user_id, game_id, bet_amount, result, profit) VALUES (?, ?, ?, ?, ?)',
                (pid, self.chat_id, self.bet_amount, result, profit)
            )
        await db_connection.commit()
        
        # Remove game from active games
        if self.chat_id in active_games:
            del active_games[self.chat_id]
        
        return {winner: player_win_amount}

    async def player_action(self, player_id: int, action: PlayerAction, amount: int = 0, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> Tuple[bool, str]:
        if player_id not in self.players or player_id != self.current_player:
            return False, "Сейчас не ваш ход!"
            
        player = self.players[player_id]
        
        if action == PlayerAction.FOLD:
            player['folded'] = True
            player['shown'] = True
            message = f"{player['name']} сбрасывает карты {EMOJI['skull']}"
            
            # Check if we need to show final choice (when 2 players left from 3+)
            active_players = [pid for pid, p in self.players.items() if not p['folded']]
            if len(active_players) == 2 and self.initial_player_count >= 3:
                await self.show_final_choice(context)
            else:
                await self.next_player(context)
                
            return True, message
            
        elif action == PlayerAction.RAISE:
            if amount <= self.max_bid:
                return False, "Ставка должна быть выше текущей!"
                
            if amount > player['balance'] + player['bid']:
                return False, "У вас недостаточно средств!"
                
            min_raise = max(int(self.max_bid * 0.1), 10)
            if amount - self.max_bid < min_raise:
                return False, f"Минимальное повышение: {self.max_bid + min_raise} {EMOJI['money']}"
                
            amount_to_deduct = amount - player['bid']
            await self.update_user_balance(player_id, -amount_to_deduct, "game_raise")
            player['balance'] -= amount_to_deduct
            player['bid'] = amount
            self.pot += amount_to_deduct
            self.max_bid = amount
            self.last_raiser = player_id
            message = f"{player['name']} повышает ставку до {amount}{EMOJI['money']} {EMOJI['fire']}"
            await self.next_player(context)
            return True, message
            
        elif action == PlayerAction.CALL:
            if self.max_bid == 0:
                return False, "Нельзя поддержать нулевую ставку!"
            if player['bid'] == self.max_bid:
                return False, "Вы уже сделали эту ставку!"
                
            diff = self.max_bid - player['bid']
            if diff > player['balance']:
                player['folded'] = True
                player['shown'] = True
                message = f"{player['name']} не может поддержать ставку и выбывает {EMOJI['skull']}"
                
                # Check if we need to show final choice (when 2 players left from 3+)
                active_players = [pid for pid, p in self.players.items() if not p['folded']]
                if len(active_players) == 2 and self.initial_player_count >= 3:
                    await self.show_final_choice(context)
                else:
                    await self.next_player(context)
            else:
                await self.update_user_balance(player_id, -diff, "game_call")
                player['balance'] -= diff
                self.pot += diff
                player['bid'] = self.max_bid
                message = f"{player['name']} поддерживает ставку {EMOJI['check']}"
                await self.next_player(context)
            return True, message
            
        elif action == PlayerAction.CHECK:
            if self.max_bid != 0 and player['bid'] < self.max_bid:
                return False, "Нельзя пропустить ход при активной ставке!"
            message = f"{player['name']} пропускает ход {EMOJI['clock']}"
            await self.next_player(context)
            return True, message
            
        elif action == PlayerAction.SHOW:
            if not self.last_raiser:
                return False, "Нельзя вскрыться на первом круге торгов!"
            if player['bid'] != self.max_bid:
                return False, "Вы должны поддержать ставку перед вскрытием!"
                
            player['shown'] = True
            self.last_raiser = None
            self.state = GameState.AWAITING_CONFIRMATION
            self.show_confirmations = {player_id}
            message = f"{player['name']} требует вскрытия! Ожидаем подтверждения остальных участников."
            return True, message
            
        elif action == PlayerAction.LOOK:
            return False, "Режим игры вслепую отключен"
            
        elif action == PlayerAction.SWARA:
            active_players = [pid for pid, p in self.players.items() if not p['folded']]
            if len(active_players) < 2:
                return False, "Недостаточно игроков для свары."
                
            idx = active_players.index(player_id)
            right_idx = (idx + 1) % len(active_players)
            opponent_id = active_players[right_idx]
            opponent = self.players[opponent_id]
            
            challenger_score = self.calculate_hand_value(player['cards'])
            opponent_score = self.calculate_hand_value(opponent['cards'])
            
            if challenger_score > opponent_score:
                opponent['folded'] = True
                message = (f"{player['name']} выиграл свару против {opponent['name']} "
                           f"(его {challenger_score} > {opponent_score}) и остается в игре.")
                
                # Check if only one player left after swara
                active_players = [pid for pid, p in self.players.items() if not p['folded']]
                if len(active_players) == 1:
                    self.state = GameState.SHOWDOWN
                    await self.determine_winner(context)
                else:
                    await self.next_player(context)
            else:
                player['folded'] = True
                message = (f"{player['name']} проиграл свару против {opponent['name']} "
                           f"(его {challenger_score} <= {opponent_score}) и выбывает.")
                
                # Check if only one player left after swara
                active_players = [pid for pid, p in self.players.items() if not p['folded']]
                if len(active_players) == 1:
                    self.state = GameState.SHOWDOWN
                    await self.determine_winner(context)
                else:
                    await self.next_player(context)
            return True, message
                
        return False, "Неизвестное действие"
    
    async def show_final_choice(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show options when 2 players left from 3+ initial players"""
        active_players = [pid for pid, p in self.players.items() if not p['folded']]
        if len(active_players) != 2 or self.initial_player_count < 3:
            return
            
        self.state = GameState.FINAL_CHOICE
        self.final_choice_set = set()
        
        # Отправляем интерфейс выбора только игрокам (без сообщения в групповой чат)
        for pid in active_players:
            try:
                await send_player_interface(context, pid, self)
            except Exception as e:
                logger.error(f"Ошибка отправки интерфейса игроку {pid}: {e}")
    
    async def handle_final_choice(self, player_id: int, choice: str, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle player's choice in final 2 players situation"""
        if self.state != GameState.FINAL_CHOICE or player_id not in self.players:
            return
            
        active_players = [pid for pid, p in self.players.items() if not p['folded']]
        if len(active_players) != 2:
            return
            
        self.final_choice_set.add(player_id)
        
        if choice == "final_swara":
            self.final_swara_set.add(player_id)
            await context.bot.send_message(
                chat_id=self.chat_id,
                text=f"{self.players[player_id]['name']} выбрал свару ⚔️"
            )
        elif choice == "final_split":
            self.final_split_set.add(player_id)
            await context.bot.send_message(
                chat_id=self.chat_id,
                text=f"{self.players[player_id]['name']} выбрал раздел банка 🤝"
            )
            # If both players chose to split, immediately split the pot
            if len(self.final_split_set) == 2:
                await self.split_pot(context)
                return
        elif choice == "final_continue":
            await context.bot.send_message(
                chat_id=self.chat_id,
                text=f"{self.players[player_id]['name']} выбрал продолжение игры 🔄"
            )
            
        # If both players made a choice
        if len(self.final_choice_set) == 2:
            # Try to delete the original message
            try:
                if self.swara_message_id:
                    await context.bot.delete_message(
                        chat_id=self.chat_id,
                        message_id=self.swara_message_id
                    )
            except Exception as e:
                logger.error(f"Ошибка удаления сообщения: {e}")
            
            # If at least one player chose swara, start swara recruitment
            if len(self.final_swara_set) >= 1:
                await self.start_swara_recruitment(context)
            # If both chose split, split the pot (already handled above)
            elif len(self.final_split_set) == 2:
                pass  # Already handled
            # Otherwise continue game with just these 2 players
            else:
                self.state = GameState.BIDDING
                self.current_player = active_players[0]
                await context.bot.send_message(
                    chat_id=self.chat_id,
                    text="🔄 Игра продолжается между двумя оставшимися игроками!"
                )
                
                for pid in active_players:
                    try:
                        await send_player_interface(context, pid, self)
                    except Exception as e:
                        logger.error(f"Ошибка отправки интерфейса игроку {pid}: {e}")
    
    async def start_swara_recruitment(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start swara recruitment phase"""
        self.state = GameState.FINAL_SWARA_WAIT
        self.swara_participants = set(pid for pid, p in self.players.items() if not p['folded'])
        self.swara_cost = self.pot // 2  # 50% of current pot
        
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=(
                f"⚔️ Начинается набор в свару!\n\n"
                f"💰 Стоимость входа: {self.swara_cost} {EMOJI['money']} (50% от банка)\n"
                f"👥 Текущие участники: {len(self.swara_participants)}/{MAX_PLAYERS}\n"
                f"⏳ Набор продлится 60 секунд\n\n"
                f"Для присоединения нажмите кнопку ниже"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"⚔️ Присоединиться ({self.swara_cost} {EMOJI['money']})", 
                    callback_data="join_swara"
                )]
            ])
        )
        
        # Set timeout for swara recruitment
        self.timeout_job = context.job_queue.run_once(
            self.start_swara_round,
            60,
            chat_id=self.chat_id,
            name=f"start_swara_{self.chat_id}"
        )
    
    async def start_swara_round(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Start the actual swara round with all participants"""
        if len(self.swara_participants) < 2:
            await context.bot.send_message(
                chat_id=self.chat_id,
                text="❌ Недостаточно участников для свары. Игра продолжается в обычном режиме."
            )
            self.state = GameState.BIDDING
            active_players = [pid for pid, p in self.players.items() if not p['folded']]
            self.current_player = active_players[0] if active_players else None
            return
            
        # Collect fees from all participants
        for pid in self.swara_participants:
            if pid not in self.players:
                continue
                
            player = self.players[pid]
            if await self.update_user_balance(pid, -self.swara_cost, "swara_fee"):
                player['balance'] -= self.swara_cost
                self.pot += self.swara_cost
    
        # Reset game state for swara round with proper variable usage
        for pid, player in self.players.items():
            player['folded'] = (pid not in self.swara_participants)
            player['shown'] = False
            player['bid'] = 0
    
        self.shuffle_deck()
        self.deal_cards()
        self.state = GameState.BIDDING
        self.current_player = next(iter(self.swara_participants))
        self.max_bid = 0
        self.last_raiser = None
        
        participants_names = ", ".join(self.players[pid]['name'] for pid in self.swara_participants)
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=(
                f"⚔️ Свара начинается!\n\n"
                f"👥 Участники: {participants_names}\n"
                f"💰 Банк: {self.pot} {EMOJI['money']}\n"
                f"🎴 Разданы новые карты\n\n"
                f"🔄 Первый ход: {self.players[self.current_player]['name']}"
            )
        )
        
        for pid in self.swara_participants:
            try:
                await send_player_interface(context, pid, self)
            except Exception as e:
                logger.error(f"Ошибка отправки интерфейса игроку {pid}: {e}")

    async def handle_swara_join(self, query, user_id, context: ContextTypes.DEFAULT_TYPE):
        """Handle player joining swara"""
        if self.state != GameState.FINAL_SWARA_WAIT:
            return
            
        if user_id in self.swara_participants:
            await query.answer("Вы уже участвуете в сваре", show_alert=True)
            return
            
        # Check balance
        player_balance = await self.get_user_balance(user_id)
        if player_balance < self.swara_cost:
            await query.answer(
                f"⚠️ Недостаточно средств. Нужно: {self.swara_cost} {EMOJI['money']}", 
                show_alert=True
            )
            return
            
        # Add new player if not already in game
        if user_id not in self.players:
            if len(self.players) >= MAX_PLAYERS:
                await query.answer("Достигнут максимум игроков", show_alert=True)
                return
                
            if not await self.add_player(user_id, query.from_user.full_name):
                await query.answer("Ошибка добавления в игру", show_alert=True)
                return
    
        # Add to swara participants
        self.swara_participants.add(user_id)
        
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=f"⚔️ {self.players[user_id]['name']} присоединился к сваре за {self.swara_cost} {EMOJI['money']}!"
        )
        
        # Update player interface
        await send_player_interface(context, user_id, self)
        
        # Check if we reached max players
        if len(self.swara_participants) >= MAX_PLAYERS:
            if self.timeout_job:
                self.timeout_job.schedule_removal()
            await self.start_swara_round(context)

    async def split_pot(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Split pot between last 2 players"""
        active_players = [pid for pid, p in self.players.items() if not p['folded']]
        if len(active_players) != 2:
            return
            
        player1_id = active_players[0]
        player2_id = active_players[1]
        
        # Calculate amounts with dealer fee
        total = self.pot
        dealer_fee = int(total * DEALER_FEE_PERCENT / 100)
        player_share = (total - dealer_fee) // 2
        
        # Distribute funds
        await self.update_user_balance(player1_id, player_share, "game_split")
        await self.update_user_balance(player2_id, player_share, "game_split")
        await self.update_user_balance(0, dealer_fee, "dealer_fee")
        
        # Show cards
        cards_text = []
        for pid, p in self.players.items():
            hand_value = self.calculate_hand_value(p['cards'])
            cards_text.append(f"🃏 {p['name']}: {' '.join(str(c) for c in p['cards'])} (сила: {hand_value})")
        
        await context.bot.send_message(
            chat_id=self.chat_id,
            text=(
                f"🤝 Игроки договорились разделить банк!\n\n"
                f"🏆 Каждый получает: {player_share} {EMOJI['money']}\n"
                f"🏦 Комиссия дилера: {dealer_fee} {EMOJI['money']}\n\n"
                "Карты игроков:\n" + "\n".join(cards_text) +
                "\n\n🔄 Для новой игры напишите /begin или /fast"
            )
        )
        
        # Send private messages
        for pid in self.players:
            try:
                if pid in active_players:
                    message = (
                        f"🤝 Вы разделили банк и получаете {player_share} {EMOJI['money']}!\n"
                        f"🏦 Комиссия дилера: {dealer_fee} {EMOJI['money']}\n\n"
                        f"Ваши карты: {self.get_player_cards(pid)}\n"
                        f"Сила руки: {self.calculate_hand_value(self.players[pid]['cards'])}\n\n"
                        f"💵 Ваш выигрыш зачислен на баланс."
                    )
                else:
                    message = (
                        f"😢 Вы выбыли из игры.\n\n"
                        f"Ваши карты: {self.get_player_cards(pid)}\n"
                        f"Сила руки: {self.calculate_hand_value(self.players[pid]['cards'])}\n\n"
                        f"Вы потеряли ставку: {self.bet_amount} {EMOJI['money']}"
                    )
                
                await context.bot.send_message(chat_id=pid, text=message)
            except Exception as e:
                logger.error(f"Ошибка отправки личного сообщения игроку {pid}: {e}")
        
        self.state = GameState.FINISHED
        
        # Record game results
        for pid in self.players:
            result = 'win' if pid in active_players else 'lose'
            profit = player_share if pid in active_players else -self.bet_amount
            await db_connection.execute(
                'INSERT INTO game_history (user_id, game_id, bet_amount, result, profit) VALUES (?, ?, ?, ?, ?)',
                (pid, self.chat_id, self.bet_amount, result, profit)
            )
        await db_connection.commit()
        
        # Remove game from active games
        if self.chat_id in active_games:
            del active_games[self.chat_id]
    
    async def next_player(self, context: Optional[ContextTypes.DEFAULT_TYPE] = None) -> None:
        active_players = [pid for pid, p in self.players.items() if not p['folded'] and not p['shown']]
        
        # Check if we have 2 players left from 3+ initial players
        if len(active_players) == 2 and self.initial_player_count >= 3:
            await self.show_final_choice(context)
            return
            
        if len(active_players) <= 1:
            self.state = GameState.SHOWDOWN
            if context:
                await self.determine_winner(context)
            return
            
        current_idx = active_players.index(self.current_player) if self.current_player in active_players else -1
        next_idx = (current_idx + 1) % len(active_players)
        self.current_player = active_players[next_idx]
    
    async def get_user_balance(self, user_id: int) -> int:
        try:
            async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error(f"Ошибка при получении баланса: {e}")
            return 0
    
    async def update_user_balance(self, user_id: int, amount: int, transaction_type: str) -> bool:
        try:
            async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
                row = await cursor.fetchone()
                
            if row is None:
                new_balance = amount
                await db_connection.execute('INSERT INTO user_balances (user_id, balance) VALUES (?, ?)', (user_id, new_balance))
            else:
                current_balance = row[0]
                new_balance = current_balance + amount
                await db_connection.execute('UPDATE user_balances SET balance = ? WHERE user_id = ?', (new_balance, user_id))
                
            await db_connection.execute(
                'INSERT INTO transactions (user_id, amount, transaction_type, game_id) VALUES (?, ?, ?, ?)',
                (user_id, amount, transaction_type, self.chat_id)
            )
            await db_connection.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка при обновлении баланса: {e}")
            return False

    def get_player_cards(self, user_id: int) -> str:
        if user_id in self.players:
            return " ".join(str(card) for card in self.players[user_id]['cards'])
        return ""

# Глобальный словарь активных игр
active_games: Dict[int, SekaGame] = {}

async def create_crypto_bot_invoice(user_id: int, amount: float) -> Optional[str]:
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "user_id": user_id,
        "amount": amount,
        "asset": "USDT",
        "description": f"Deposit for user {user_id}",
        "hidden_message": "Thank you for your deposit!",
        "expires_in": 3600
    }
    try:
        response = requests.post(
            f"{CRYPTO_BOT_API_URL}createInvoice",
            headers=headers,
            json=payload,
            timeout=10
        )
        if response.status_code == 200 and response.json().get("ok"):
            return response.json()["result"]["pay_url"]
    except Exception as e:
        logger.error(f"Error creating Crypto Bot invoice: {e}")
    return None

async def process_crypto_bot_withdrawal(user_id: int, amount: float, wallet: str) -> bool:
    headers = {
        "Crypto-Pay-API-Token": CRYPTO_BOT_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "user_id": user_id,
        "asset": "USDT",
        "amount": amount,
        "address": wallet,
        "comment": f"Withdrawal for user {user_id}"
    }
    try:
        response = requests.post(
            f"{CRYPTO_BOT_API_URL}transfer",
            headers=headers,
            json=payload,
            timeout=10
        )
        return response.status_code == 200 and response.json().get("ok", False)
    except Exception as e:
        logger.error(f"Error processing Crypto Bot withdrawal: {e}")
    return False

async def send_player_interface(context: ContextTypes.DEFAULT_TYPE, player_id: int, game: SekaGame) -> None:
    player = game.players[player_id]
    keyboard = []
    
    state_titles = {
        GameState.WAITING: "⏳ Ожидание игроков",
        GameState.JOINING: "➕ Присоединение",
        GameState.BIDDING: "💰 Торги",
        GameState.AWAITING_CONFIRMATION: "🔄 Подтверждение вскрытия",
        GameState.FINAL_CHOICE: "⚡ Выбор действия",
        GameState.FINAL_SWARA_WAIT: "⚔️ Ожидание свары",
        GameState.SHOWDOWN: "🎯 Определение победителя",
        GameState.FINISHED: "🏁 Игра завершена"
    }
    
    if game.state == GameState.AWAITING_CONFIRMATION:
        confirm_buttons = []
        if player_id not in game.show_confirmations:
            confirm_buttons.append(InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_show"))
        else:
            confirm_buttons.append(InlineKeyboardButton("🔄 Вы подтвердили", callback_data="noop"))
        
        confirm_buttons.append(InlineKeyboardButton("❌ Отказаться", callback_data="decline_show"))
        keyboard.append(confirm_buttons)
            
    elif game.state == GameState.FINAL_SWARA_WAIT:
        if player_id not in game.swara_participants:
            keyboard.append([InlineKeyboardButton(
                f"⚔️ Присоединиться ({game.swara_cost} {EMOJI['money']})", 
                callback_data="join_swara"
            )])
            
    elif game.state == GameState.FINAL_CHOICE:
        active_players = [pid for pid, p in game.players.items() if not p['folded']]
        if player_id in active_players:
            keyboard = [
                [InlineKeyboardButton("⚔️ Начать свару", callback_data="final_swara")],
                [InlineKeyboardButton("🤝 Разделить банк", callback_data="final_split")],
                [InlineKeyboardButton("🔄 Продолжить игру", callback_data="final_continue")]
            ]
            
    elif game.state == GameState.BIDDING and game.current_player == player_id:
        min_raise = max(int(game.max_bid * 0.1), 10)
        possible_raises = [
            game.max_bid + min_raise,
            game.max_bid + min_raise * 2,
            game.max_bid + min_raise * 3
        ]
        
        raise_buttons = []
        for amount in possible_raises:
            if amount <= player['balance'] + player['bid']:
                raise_buttons.append(InlineKeyboardButton(f"🔼 +{amount-game.max_bid}", callback_data=f"raise_{amount}"))
        
        if raise_buttons:
            keyboard.append(raise_buttons)
        
        action_buttons = []
        
        if game.max_bid > player['bid']:
            call_amount = game.max_bid - player['bid']
            action_buttons.append(InlineKeyboardButton(
                f"✅ Поддержать (+{call_amount})", 
                callback_data="call"
            ))
        else:
            action_buttons.append(InlineKeyboardButton("⏭ Пропустить", callback_data="check"))
            
        action_buttons.append(InlineKeyboardButton("❌ Сбросить", callback_data="fold"))
        
        if game.last_raiser and game.max_bid == player['bid']:
            action_buttons.append(InlineKeyboardButton("🃏 Вскрыться", callback_data="show"))
            
        keyboard.append(action_buttons)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    mode_info = {
        GameMode.NORMAL: "🎮 Обычная игра",
        GameMode.FAST: "⚡ Быстрая игра",
        GameMode.TOURNAMENT: "🏆 Турнир"
    }.get(game.game_mode, "🎮 Обычная игра")
    
    cards_display = game.get_player_cards(player_id)
    
    text = (
        f"{mode_info}\n"
        f"🏷 Ставка: {game.bet_amount} {EMOJI['money']}\n"
        f"💵 Ваш баланс: {player['balance']} {EMOJI['money']}\n"
        f"🏦 Банк: {game.pot} {EMOJI['money']}\n\n"
        f"🎴 Ваши карты: {cards_display}\n"
    )
    
    if game.state == GameState.FINISHED:
        text += "\n🎉 Игра завершена! Ожидайте новую игру."
    elif game.state == GameState.AWAITING_CONFIRMATION:
        confirmed = len(game.show_confirmations)
        total = len([p for p in game.players.values() if not p['folded']])
        text += f"\n🔄 Ожидание подтверждения вскрытия: {confirmed}/{total}"
    elif game.state == GameState.FINAL_CHOICE:
        text += "\n⚡ Осталось 2 игрока! Выберите действие:"
    elif game.state == GameState.FINAL_SWARA_WAIT:
        if player_id in game.swara_participants:
            text += "\n⚔️ Вы участвуете в сваре. Ожидание..."
        else:
            text += f"\n⚔️ Можно присоединиться за {game.swara_cost} {EMOJI['money']}"
    elif game.current_player == player_id:
        text += "\n🔄 Сейчас ваш ход!"
    else:
        text += f"\n⏳ Ход игрока: {game.players[game.current_player]['name']}"
    
    if game.state == GameState.BIDDING and game.current_player == player_id:
        text += "\n\nℹ️ Выберите действие:"
        
    try:
        if player['message_id']:
            await context.bot.edit_message_text(
                chat_id=player_id,
                message_id=player['message_id'],
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        else:
            msg = await context.bot.send_message(
                chat_id=player_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
            player['message_id'] = msg.message_id
    except Exception as e:
        logger.error(f"Ошибка отправки интерфейса игроку {player_id}: {e}")

async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    
    game = None
    for g in active_games.values():
        if user_id in g.players:
            game = g
            break
            
    if not game:
        await query.edit_message_text("❌ Игра не найдена или завершена.")
        return

    try:
        if data in ("final_swara", "final_split", "final_continue"):
            await game.handle_final_choice(user_id, data, context)
            return
            
        if data == "join_swara":
            await game.handle_swara_join(query, user_id, context)
            return
            
        if data == "confirm_show":
            await handle_show_confirmation(query, user_id, game, context)
            return
            
        if data == "decline_show":
            await handle_show_decline(query, user_id, game, context)
            return
            
        if data == "noop":
            return

        action_result = None
        action_mapping = {
            'fold': (PlayerAction.FOLD, 0, "❌ Сбросил карты"),
            'call': (PlayerAction.CALL, 0, "✅ Поддержал ставку"),
            'check': (PlayerAction.CHECK, 0, "⏭ Пропустил ход"),
            'show': (PlayerAction.SHOW, 0, "🃏 Требует вскрытия!"),
            'look': (PlayerAction.LOOK, 0, "👀 Посмотрел карты"),
            'swara': (PlayerAction.SWARA, 0, "⚔️ Начинает свару!")
        }
        
        if data.startswith('raise_'):
            amount = int(data.split('_')[1])
            action_result = await game.player_action(user_id, PlayerAction.RAISE, amount, context)
        elif data in action_mapping:
            action, amount, default_msg = action_mapping[data]
            action_result = await game.player_action(user_id, action, amount, context)
            if action_result[0] and action == PlayerAction.SWARA:
                action_result = (action_result[0], default_msg)
        
        if action_result:
            success, message = action_result
            if success:
                await context.bot.send_message(
                    chat_id=game.chat_id,
                    text=f"{game.players[user_id]['name']}: {message}"
                )
            else:
                await query.answer(message, show_alert=True)
            for pid in game.players:
                try:
                    await send_player_interface(context, pid, game)
                except Exception as e:
                    logger.error(f"Ошибка обновления интерфейса игрока {pid}: {e}")
        else:
            await query.answer("❌ Неизвестное действие", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка обработки действия: {e}")
        await query.answer("❌ Произошла ошибка. Попробуйте еще раз.", show_alert=True)

async def handle_swara_join(query, user_id, game, context):
    if game.state == GameState.FINAL_SWARA_WAIT and user_id not in game.swara_participants:
        player_balance = await game.get_user_balance(user_id)
        if player_balance >= game.swara_cost:
            if await game.update_user_balance(user_id, -game.swara_cost, "join_swara"):
                game.swara_participants.add(user_id)
                await context.bot.send_message(
                    chat_id=game.chat_id,
                    text=f"⚔️ {game.players[user_id]['name']} присоединился к сваре за {game.swara_cost} {EMOJI['money']}!"
                )
                await send_player_interface(context, user_id, game)
            else:
                await query.answer("❌ Ошибка при списании средств", show_alert=True)
        else:
            await query.answer(
                f"⚠️ Недостаточно средств. Нужно: {game.swara_cost} {EMOJI['money']}", 
                show_alert=True
            )

async def handle_show_confirmation(query, user_id, game, context):
    if game.state == GameState.AWAITING_CONFIRMATION and user_id not in game.show_confirmations:
        game.show_confirmations.add(user_id)
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=f"🔄 {game.players[user_id]['name']} подтвердил вскрытие карт."
        )
        
        active_players = [pid for pid, p in game.players.items() if not p['folded']]
        if set(active_players) == game.show_confirmations:
            game.state = GameState.SHOWDOWN
            await context.bot.send_message(
                chat_id=game.chat_id,
                text="🎯 Все игроки подтвердили вскрытие! Определяем победителя..."
            )
            await game.determine_winner(context)

async def handle_show_decline(query, user_id, game, context):
    game.state = GameState.BIDDING
    game.show_confirmations.clear()
    await context.bot.send_message(
        chat_id=game.chat_id,
        text=f"🔄 {game.players[user_id]['name']} отказался от вскрытия. Продолжаем торги."
    )
    for pid in game.players:
        try:
            await send_player_interface(context, pid, game)
        except Exception as e:
            logger.error(f"Ошибка обновления интерфейса игрока {pid}: {e}")

async def start_game_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    bet_amount = context.job.data['bet_amount']
    
    if chat_id not in active_games:
        return
        
    game = active_games[chat_id]
    
    if len(game.players) < 2:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="❌ Недостаточно игроков для начала (минимум 2). Игра отменена."
        )
        for player_id in game.players:
            await game.update_user_balance(player_id, game.bet_amount, "game_cancel")
        del active_games[chat_id]
        return
        
    if await game.start_game():
        mode_info = {
            GameMode.NORMAL: "обычном режиме",
            GameMode.FAST: "быстром режиме",
            GameMode.TOURNAMENT: "турнирном режиме"
        }.get(game.game_mode, "обычном режиме")
        
        players_list = "\n".join(
            f"• {p['name']}"
            for p in game.players.values()
        )
        
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎮 Игра началась! ({mode_info})\n"
                f"💰 Ставка: {bet_amount} {EMOJI['money']}\n"
                f"🏦 Банк: {game.pot} {EMOJI['money']}\n\n"
                f"👥 Участники ({len(game.players)}):\n{players_list}\n\n"
                f"🔄 Первый ход: {game.players[game.current_player]['name']}"
            )
        )
        
        keyboard = [[InlineKeyboardButton("🎮 Перейти к игре", url=f"https://t.me/{context.bot.username}")]]
        await message.edit_reply_markup(InlineKeyboardMarkup(keyboard))
        
        for player_id in game.players:
            try:
                await send_player_interface(context, player_id, game)
            except Exception as e:
                logger.error(f"Ошибка отправки интерфейса игроку {player_id}: {e}")
    else:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="❌ Не удалось начать игру. Попробуйте позже."
        )
        del active_games[chat_id]

async def start_fast_game_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    bet_amount = context.job.data['bet_amount']
    
    if chat_id not in active_games:
        return
        
    game = active_games[chat_id]
    
    if len(game.players) < 2:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="❌ Недостаточно игроков для начала (минимум 2). Игра отменена."
        )
        for player_id in game.players:
            await game.update_user_balance(player_id, game.bet_amount, "game_cancel")
        del active_games[chat_id]
        return
        
    if await game.start_game():
        players_list = "\n".join(
            f"• {p['name']}"
            for p in game.players.values()
        )
        
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚡ Быстрая игра началась!\n"
                f"💰 Фиксированная ставка: {bet_amount} {EMOJI['money']}\n"
                f"🏦 Банк: {game.pot} {EMOJI['money']}\n\n"
                f"👥 Участники ({len(game.players)}):\n{players_list}\n\n"
                f"🎯 Определяем победителя..."
            )
        )
        
        # Immediately go to showdown and determine winner
        if game.state == GameState.SHOWDOWN:
            await game.determine_winner(context)
    else:
        await context.bot.send_message(
            chat_id=chat_id, 
            text="❌ Не удалось начать игру. Попробуйте позже."
        )
        del active_games[chat_id]

async def private_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
        
    user_id = update.effective_user.id
    await db_connection.execute(
        'INSERT OR IGNORE INTO user_balances (user_id, balance) VALUES (?, ?)', (user_id, DEFAULT_BALANCE)
    )
    await db_connection.commit()
    
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['profile']} Профиль", callback_data="profile")],
        [InlineKeyboardButton(f"{EMOJI['balance']} Баланс", callback_data="balance")],
        [InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data="deposit"),
         InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(f"{EMOJI['add']} Добавить в чат", callback_data="add_to_chat")],
        [InlineKeyboardButton(f"{EMOJI['top']} Топ игроков", callback_data="top_players")],
        [InlineKeyboardButton(f"{EMOJI['info']} Информация", callback_data="info")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text("🏠 Главное меню:", reply_markup=reply_markup)

async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    
    if data == "profile":
        await show_profile(query, user_id, context)
    elif data == "balance":
        await show_balance_menu(query, user_id, context)
    elif data == "deposit":
        await handle_deposit(update, context)
    elif data == "withdraw":
        await handle_withdraw(update, context)
    elif data == "add_to_chat":
        await show_add_to_chat_instructions(query)
    elif data == "top_players":
        await show_top_players(query, context)
    elif data == "info":
        await show_info(query)
    elif data == "back_to_menu":
        await show_main_menu_from_query(query)
    elif data == "admin_menu":
        await show_admin_menu(query, context)

async def show_profile(query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        async with db_connection.execute('SELECT COUNT(*) FROM game_history WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        games_played = row[0] if row else 0
        
        async with db_connection.execute('SELECT COUNT(*) FROM game_history WHERE user_id = ? AND result = "win"', (user_id,)) as cursor:
            row = await cursor.fetchone()
        games_won = row[0] if row else 0
        
        async with db_connection.execute('SELECT SUM(amount) FROM transactions WHERE user_id = ? AND amount > 0', (user_id,)) as cursor:
            row = await cursor.fetchone()
        total_profit = row[0] if row and row[0] is not None else 0
        
        await query.edit_message_text(
            f"👤 Ваш профиль:\n\n"
            f"🆔 ID: {user_id}\n"
            f"💰 Баланс: {balance} {EMOJI['money']}\n"
            f"🎮 Игр сыграно: {games_played}\n"
            f"🏆 Побед: {games_won}\n"
            f"💵 Общий выигрыш: {total_profit} {EMOJI['money']}\n\n",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]
            ])
        )
    except Exception as e:
        logger.error(f"Ошибка при показе профиля: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке профиля. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )

async def show_balance_menu(query, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        await query.edit_message_text(
            f"💰 Ваш баланс: {balance} {EMOJI['money']}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data="deposit"),
                 InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data="withdraw")],
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]
            ])
        )
    except Exception as e:
        logger.error(f"Ошибка при показе баланса: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке баланса. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )

async def show_add_to_chat_instructions(query) -> None:
    await query.edit_message_text(
        "📌 Как добавить бота в чат:\n\n"
        "1. Откройте нужный чат\n"
        "2. Нажмите на название чата вверху\n"
        "3. Выберите 'Добавить участников'\n"
        "4. Найдите @SekaPlaybot и добавьте\n\n"
        "После добавления напишите /start в чате для активации бота.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
    )

async def show_top_players(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with db_connection.execute('SELECT user_id, balance FROM user_balances ORDER BY balance DESC LIMIT 10') as cursor:
            top_players = await cursor.fetchall()
            
        if not top_players:
            await query.edit_message_text(
                "🏆 Топ игроков пока пуст. Будьте первым!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
            )
            return
            
        text = "🏆 Топ игроков по балансу:\n\n"
        for i, player in enumerate(top_players, 1):
            uid, balance = player
            try:
                user = await context.bot.get_chat(uid)
                name = user.full_name
            except:
                name = f"Игрок {uid}"
                
            async with db_connection.execute('SELECT COUNT(*) FROM game_history WHERE user_id = ?', (uid,)) as cursor:
                row = await cursor.fetchone()
            games_played = row[0] if row else 0
            text += f"{i}. {name} - {balance} {EMOJI['money']} (игр: {games_played})\n"
            
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )
    except Exception as e:
        logger.error(f"Ошибка при показе топа игроков: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при загрузке топа игроков. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )

async def show_info(query) -> None:
    await query.edit_message_text(
        "ℹ️ Информация и правила игры:\n\n"
        "🎴 <b>Seka</b> - карточная игра с элементами покера и блефа.\n\n"
        "📌 <b>Основные правила:</b>\n"
        "- Каждый игрок получает 3 карты\n"
        "- Джокер - трефовая семерка (JOK), усиливает любую масть\n"
        "- Три шестерки - особая комбинация (34 очка)\n"
        "- Два туза дают 22 очка\n\n"
        "💰 <b>Игра на деньги:</b>\n"
        "- Для начала игры нужен положительный баланс\n"
        "- Создатель игры указывает ставку\n"
        "- Участники должны иметь сумму не меньше ставки\n"
        "- Выигрыш зачисляется на баланс за вычетом 20% комиссии дилера\n\n"
        "🔄 <b>Как начать игру:</b>\n"
        "1. Добавьте бота в группу\n"
        "2. Напишите /start для регистрации\n"
        "3. Создатель пишет /begin и указывает ставку\n"
        "4. Или выберите режим: /fast\n\n",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
    )

async def show_main_menu_from_query(query):
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['profile']} Профиль", callback_data="profile")],
        [InlineKeyboardButton(f"{EMOJI['balance']} Баланс", callback_data="balance")],
        [InlineKeyboardButton(f"{EMOJI['deposit']} Пополнить", callback_data="deposit"),
         InlineKeyboardButton(f"{EMOJI['withdraw']} Вывести", callback_data="withdraw")],
        [InlineKeyboardButton(f"{EMOJI['add']} Добавить в чат", callback_data="add_to_chat")],
        [InlineKeyboardButton(f"{EMOJI['top']} Топ игроков", callback_data="top_players")],
        [InlineKeyboardButton(f"{EMOJI['info']} Информация", callback_data="info")]
    ]
    await query.edit_message_text("🏠 Главное меню:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("500 ₽", callback_data="deposit_500"),
         InlineKeyboardButton("1000 ₽", callback_data="deposit_1000")],
        [InlineKeyboardButton("3000 ₽", callback_data="deposit_3000"),
         InlineKeyboardButton("5000 ₽", callback_data="deposit_5000")],
        [InlineKeyboardButton("10000 ₽", callback_data="deposit_10000"),
         InlineKeyboardButton("20000 ₽", callback_data="deposit_20000")],
        [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]
    ]
    
    await query.edit_message_text(
        "📥 Выберите сумму для пополнения:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
    except Exception as e:
        logger.error(f"Error getting balance for withdraw: {e}")
        await query.edit_message_text(
            "❌ Произошла ошибка при проверке баланса. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]]))
        return
        
    if balance < MIN_WITHDRAW:
        await query.edit_message_text(
            f"❌ Минимальная сумма вывода: {MIN_WITHDRAW} ₽\nВаш баланс: {balance} ₽",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )
        return
        
    await query.edit_message_text(
        f"📤 Для вывода средств укажите:\n\n"
        f"1. Сумму в рублях (не менее {MIN_WITHDRAW} ₽)\n"
        f"2. Ваш крипто-кошелек (USDT TRC20)\n\n"
        f"Пример:\n<code>7500 ₽: TAbCdEfGhIjKlMnOpQrStUvWxYz123456</code>\n\n"
        f"Ваш текущий баланс: {balance} ₽",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
    )
    context.user_data['awaiting_withdrawal'] = True

async def process_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    amount_rub = int(query.data.split('_')[1])
    rate = await get_exchange_rate("RUB", "USDT")
    usdt_amount = round(amount_rub * rate, 4)
    invoice_url = await create_crypto_bot_invoice(user_id, usdt_amount)
    
    if invoice_url:
        await query.edit_message_text(
            f"📥 Для пополнения на {amount_rub} ₽ (~ {usdt_amount} USDT):\n\n"
            f"1. Перейдите по ссылке: {invoice_url}\n"
            f"2. Оплатите счет в криптовалюте USDT\n"
            f"3. Средства будут зачислены автоматически по текущему курсу Binance\n\n"
            f"Текущий курс: 1 ₽ ≈ {rate:.6f} USDT\n"
            f"Обычно зачисление занимает 1-5 минут.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Проверить баланс", callback_data="balance")],
                [InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]
            ])
        )
    else:
        await query.edit_message_text(
            "❌ Не удалось создать счет для оплаты. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )

async def handle_withdrawal_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('awaiting_withdrawal', False):
        return
        
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    user_name = update.effective_user.full_name
    
    try:
        # Парсим сообщение с запросом на вывод
        parts = text.split(':')
        if len(parts) != 2:
            raise ValueError("Неверный формат")
            
        amount_part = parts[0].strip()
        amount_str = re.sub(r'[^\d.]', '', amount_part)
        if not amount_str:
            raise ValueError("Неверная сумма")
            
        amount_rub = float(amount_str)
        wallet = parts[1].strip()
        
        # Проверяем баланс пользователя
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        if balance < amount_rub:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} ₽",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
            )
            return
            
        if amount_rub < MIN_WITHDRAW:
            await update.message.reply_text(
                f"❌ Минимальная сумма вывода: {MIN_WITHDRAW} ₽",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
            )
            return

        # Списываем средства сразу при создании заявки
        await db_connection.execute(
            'UPDATE user_balances SET balance = balance - ? WHERE user_id = ?',
            (amount_rub, user_id)
        )
        # Записываем транзакцию списания
        await db_connection.execute(
            'INSERT INTO transactions (user_id, amount, transaction_type, details) VALUES (?, ?, ?, ?)',
            (user_id, -amount_rub, "withdrawal_request", f"Запрос на вывод {amount_rub}₽, кошелек: {wallet}")
        )
        # Записываем запрос на вывод в базу данных
        await db_connection.execute(
            'INSERT INTO withdrawal_requests (user_id, user_name, amount, wallet, status) VALUES (?, ?, ?, ?, ?)',
            (user_id, user_name, amount_rub, wallet, 'pending')
        )
        await db_connection.commit()
        
        # Получаем ID последнего запроса
        async with db_connection.execute('SELECT last_insert_rowid()') as cursor:
            request_id = (await cursor.fetchone())[0]
        
        # Отправляем уведомление администраторам
        for admin_id in ADMIN_IDS:
            try:
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_withdraw_{request_id}"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_withdraw_{request_id}")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"📤 Новый запрос на вывод:\n\n"
                         f"🆔 ID запроса: {request_id}\n"
                         f"👤 Пользователь: {user_name} (ID: {user_id})\n"
                         f"💰 Сумма: {amount_rub} ₽\n"
                         f"🏦 Кошелек: {wallet}\n\n"
                         f"Текущий баланс пользователя: {balance - amount_rub} ₽",
                    reply_markup=reply_markup
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления администратору {admin_id}: {e}")
        
        await update.message.reply_text(
            f"✅ Ваш запрос на вывод {amount_rub} ₽ принят в обработку.\n\n"
            f"Сумма уже списана с вашего баланса.\n"
            f"Администратор получил уведомление и обработает ваш запрос в ближайшее время.\n"
            f"Обычно обработка занимает до 24 часов.\n\n"
            f"ID вашего запроса: {request_id}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )
    except Exception as e:
        logger.error(f"Error processing withdrawal: {e}")
        await update.message.reply_text(
            "❌ Неверный формат запроса. Пример:\n\n"
            "<code>7500 ₽: TAbCdEfGhIjKlMnOpQrStUvWxYz123456</code>\n\n"
            "Укажите сумму и кошелек через двоеточие.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="back_to_menu")]])
        )
    finally:
        context.user_data['awaiting_withdrawal'] = False

async def handle_withdrawal_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id not in ADMIN_IDS:
        await query.edit_message_text("❌ У вас нет прав для этого действия.")
        return
        
    data = query.data
    action, request_id = data.split('_')[1], int(data.split('_')[2])
    
    try:
        # Получаем данные запроса
        async with db_connection.execute(
            'SELECT user_id, user_name, amount, wallet FROM withdrawal_requests WHERE id = ? AND status = "pending"',
            (request_id,)
        ) as cursor:
            request = await cursor.fetchone()
            
        if not request:
            await query.edit_message_text("❌ Запрос не найден или уже обработан.")
            return
            
        target_user_id, user_name, amount_rub, wallet = request
        
        if action == 'approve':
            # Конвертируем сумму в USDT
            rate = await get_exchange_rate("RUB", "USDT")
            usdt_amount = round(amount_rub * rate, 4)
            
            # Пытаемся выполнить вывод через Crypto Bot
            success = await process_crypto_bot_withdrawal(target_user_id, usdt_amount, wallet)
            
            if success:
                # Обновляем статус запроса
                await db_connection.execute(
                    'UPDATE withdrawal_requests SET status = "approved", processed_by = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (user_id, request_id)
                )
                await db_connection.commit()

                # Уведомляем пользователя
                try:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=f"✅ Ваш запрос на вывод #{request_id} был <b>одобрен</b> администратором!\n\n"
                             f"💰 Сумма: {amount_rub} ₽ (~{usdt_amount} USDT)\n"
                             f"🏦 Кошелек: {wallet}\n\n"
                             f"Средства отправлены. Обычно перевод занимает 15-30 минут.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления пользователю {target_user_id}: {e}")

                await query.edit_message_text(
                    f"✅ Вывод одобрен:\n\n"
                    f"🆔 ID запроса: {request_id}\n"
                    f"👤 Пользователь: {user_name} (ID: {target_user_id})\n"
                    f"💰 Сумма: {amount_rub} ₽ (~{usdt_amount} USDT)\n"
                    f"🏦 Кошелек: {wallet}\n\n"
                    f"Статус: успешно обработан"
                )
            else:
                await query.edit_message_text(
                    f"❌ Не удалось выполнить вывод через Crypto Bot.\n\n"
                    f"Запрос #{request_id} остался в статусе ожидания."
                )

        elif action == 'reject':
            # Обновляем статус запроса
            await db_connection.execute(
                'UPDATE withdrawal_requests SET status = "rejected", processed_by = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?',
                (user_id, request_id)
            )
            # Возвращаем сумму пользователю
            await db_connection.execute(
                'UPDATE user_balances SET balance = balance + ? WHERE user_id = ?',
                (amount_rub, target_user_id)
            )
            # Записываем возврат в транзакции
            await db_connection.execute(
                'INSERT INTO transactions (user_id, amount, transaction_type, details) VALUES (?, ?, ?, ?)',
                (target_user_id, amount_rub, "withdrawal_reject", f"Возврат по отклонённой заявке #{request_id}")
            )
            await db_connection.commit()

            # Уведомляем пользователя
            try:
                await context.bot.send_message(
                    chat_id=target_user_id,
                    text=f"❌ Ваш запрос на вывод #{request_id} был <b>отклонён</b> администратором.\n\n"
                         f"💰 Сумма: {amount_rub} ₽ возвращена на ваш баланс.\n"
                         f"🏦 Кошелек: {wallet}\n\n"
                         f"По вопросам обращайтесь в поддержку.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления пользователю {target_user_id}: {e}")

            await query.edit_message_text(
                f"❌ Вывод отклонен:\n\n"
                f"🆔 ID запроса: {request_id}\n"
                f"👤 Пользователь: {user_name} (ID: {target_user_id})\n"
                f"💰 Сумма: {amount_rub} ₽\n"
                f"🏦 Кошелек: {wallet}\n\n"
                f"Статус: отклонен, средства возвращены пользователю"
            )

    except Exception as e:
        logger.error(f"Ошибка обработки запроса на вывод: {e}")
        await query.edit_message_text("❌ Произошла ошибка при обработке запроса.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        f"{EMOJI['help']} <b>Доступные команды:</b>\n\n"
        f"/start - Начать игру или зарегистрироваться\n"
        f"/begin - Начать игру с указанием ставки\n"
        f"/join - Присоединиться к игре\n"
        f"/fast - Начать быструю игру (меньшие ставки)\n"
        f"/help - Показать это сообщение\n\n"
        f"🎮 <b>Действия в игре:</b>\n"
        f"- ✅ Поддержать (Call) - уравнять ставку\n"
        f"- 🔼 Повысить (Raise) - увеличить ставку\n"
        f"- ⏭ Пропустить (Check) - пропустить ход\n"
        f"- ❌ Сбросить (Fold) - выйти из раздачи\n"
        f"- 🃏 Вскрыться (Show) - инициировать вскрытие (с подтверждением)\n"
        f"- ⚔️ Свара - Дополнительный раунд\n\n"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if update.effective_chat.type == "private":
        await private_start(update, context)
        return
        
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        if balance <= 0:
            await update.message.reply_text("❌ У вас недостаточно средств для игры.")
            return
    except Exception as e:
        logger.error(f"Ошибка при проверке баланса: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
        
    if chat_id in active_games:
        game = active_games[chat_id]
        if game.state in (GameState.WAITING, GameState.JOINING):
            await update.message.reply_text(
                f"⏳ Игра ожидает начала. Ставка: {game.bet_amount} {EMOJI['money']}\nДля присоединения напишите /join"
            )
        else:
            await update.message.reply_text("⏳ Игра уже началась. Дождитесь окончания текущей игры.")
        return
        
    await update.message.reply_text(
        "🎮 Для начала игры укажите ставку в формате:\n<code>/begin сумма</code>\n\nПример:\n<code>/begin 100</code>\n\nУбедитесь, что у вас достаточно средств.",
        parse_mode="HTML"
    )

async def begin_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_games:
        await update.message.reply_text("⏳ Игра уже создана. Дождитесь окончания текущей.")
        return
        
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("❌ Укажите сумму ставки. Пример:\n<code>/begin 100</code>", parse_mode="HTML")
        return
        
    bet_amount = int(context.args[0])
    
    if bet_amount < MIN_BET:
        await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} {EMOJI['money']}")
        return
        
    if bet_amount > MAX_BET:
        await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} {EMOJI['money']}")
        return
        
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        if balance < bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} {EMOJI['money']}\nТребуется: {bet_amount} {EMOJI['money']}"
            )
            return
    except Exception as e:
        logger.error(f"Ошибка при проверке баланса: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
    except Exception as e:
        logger.error(f"Ошибка при проверке баланса: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
        
    active_games[chat_id] = SekaGame(chat_id, user_id, bet_amount)
    game = active_games[chat_id]
    
    if await game.add_player(user_id, update.effective_user.full_name):
        timeout = 30
        
        await update.message.reply_text(
            f"🎮 Игра создана! Ставка: {bet_amount} {EMOJI['money']}\n\n"
            f"Для присоединения напишите /join\n"
            f"Участники должны иметь не менее ставки\n\n"
            f"Создатель: {update.effective_user.full_name}\n"
            f"Игроков: 1\n\n"
            f"Игра начнется автоматически, когда будет 2+ участника или через {timeout} секунд."
        )
        
        game.timeout_job = context.job_queue.run_once(
            start_game_callback,
            timeout,
            chat_id=chat_id,
            name=f"start_game_{chat_id}",
            data={'bet_amount': bet_amount}
        )
    else:
        await update.message.reply_text("❌ Не удалось создать игру. Попробуйте позже.")
        return

async def start_fast_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id in active_games:
        await update.message.reply_text("⏳ Игра уже создана. Дождитесь окончания текущей.")
        return
    
    # Проверяем, указал ли пользователь сумму ставки
    if context.args and context.args[0].isdigit():
        bet_amount = int(context.args[0])
    else:
        bet_amount = FAST_MODE_BET  # Используем значение по умолчанию
    
    # Проверяем ограничения на ставку
    if bet_amount < MIN_BET:
        await update.message.reply_text(f"❌ Минимальная ставка: {MIN_BET} {EMOJI['money']}")
        return
        
    if bet_amount > MAX_BET:
        await update.message.reply_text(f"❌ Максимальная ставка: {MAX_BET} {EMOJI['money']}")
        return
    
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        if balance < bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств. Ваш баланс: {balance} {EMOJI['money']}\nТребуется: {bet_amount} {EMOJI['money']}"
            )
            return
    except Exception as e:
        logger.error(f"Ошибка при проверке баланса: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
        
    active_games[chat_id] = SekaGame(chat_id, user_id, bet_amount, GameMode.FAST)
    game = active_games[chat_id]
    
    if await game.add_player(user_id, update.effective_user.full_name):
        timeout = 30
        
        await update.message.reply_text(
            f"⚡ Быстрая игра создана! Ставка: {bet_amount} {EMOJI['money']}\n\n"
            f"Для присоединения напишите /join\n"
            f"Участники должны иметь не менее {bet_amount} {EMOJI['money']}\n\n"
            f"Создатель: {update.effective_user.full_name}\n"
            f"Игроков: 1\n\n"
            f"Игра начнется автоматически, когда будет 2-6 участников или через {timeout} секунд."
        )
        
        game.timeout_job = context.job_queue.run_once(
            start_fast_game_callback,
            timeout,
            chat_id=chat_id,
            name=f"start_fast_game_{chat_id}",
            data={'bet_amount': bet_amount}
        )
    else:
        await update.message.reply_text("❌ Не удалось создать игру. Попробуйте позже.")
        return

async def join_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in active_games:
        await update.message.reply_text("❌ Нет активной игры для присоединения. Создайте игру с помощью /begin")
        return
        
    game = active_games[chat_id]
    
    if game.state not in (GameState.WAITING, GameState.JOINING):
        await update.message.reply_text("⏳ Игра уже началась, нельзя присоединиться.")
        return
        
    if user_id in game.players:
        await update.message.reply_text("⚠️ Вы уже в игре.")
        return
        
    try:
        async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (user_id,)) as cursor:
            row = await cursor.fetchone()
        balance = row[0] if row else 0
        
        if balance < game.bet_amount:
            await update.message.reply_text(
                f"❌ Недостаточно средств для присоединения. Требуется: {game.bet_amount} {EMOJI['money']}\nВаш баланс: {balance} {EMOJI['money']}"
            )
            return
    except Exception as e:
        logger.error(f"Ошибка при проверке баланса: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        return
        
    if await game.add_player(user_id, update.effective_user.full_name):
        if game.timeout_job:
            game.timeout_job.schedule_removal()
            timeout = 30
            game.timeout_job = context.job_queue.run_once(
                start_game_callback if game.game_mode == GameMode.NORMAL else start_fast_game_callback,
                timeout,
                chat_id=chat_id,
                name=f"start_{'fast_' if game.game_mode == GameMode.FAST else ''}game_{chat_id}",
                data={'bet_amount': game.bet_amount}
            )
        
        await update.message.reply_text(
            f"🎉 {update.effective_user.full_name} присоединился к игре! {EMOJI['money']}\n"
            f"👥 Игроков: {len(game.players)}\n"
            f"💰 Ставка: {game.bet_amount} {EMOJI['money']}\n\n"
            f"Игра начнется автоматически, когда будет {'2-6' if game.game_mode == GameMode.FAST else '2+'} участников или через {timeout} секунд."
        )
        
        if len(game.players) >= 2 and game.state == GameState.WAITING:
            game.state = GameState.JOINING
    else:
        await update.message.reply_text("❌ Не удалось присоединиться к игре. Максимум 6 игроков.")

async def cancel_game(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    if chat_id not in active_games:
        await update.message.reply_text("❌ Нет активной игры для отмены.")
        return
        
    game = active_games[chat_id]
    
    if user_id != game.creator_id and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Только создатель игры или администратор может отменить игру.")
        return
        
    for player_id in game.players:
        if not game.players[player_id]['folded']:
            await game.update_user_balance(player_id, game.bet_amount, "game_cancel")
    
    await update.message.reply_text("❌ Игра отменена. Все ставки возвращены.")
    del active_games[chat_id]

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас нет прав администратора.")
        return
        
    if not context.args:
        await update.message.reply_text("Использование: /admin <команда> [аргументы]")
        return
        
    command = context.args[0].lower()
    
    if command == "balance":
        if len(context.args) < 2:
            await update.message.reply_text("Использование: /admin balance <user_id> [amount]")
            return
            
        try:
            target_id = int(context.args[1])
            if len(context.args) > 2:
                amount = int(context.args[2])
                await db_connection.execute(
                    'INSERT OR REPLACE INTO user_balances (user_id, balance) VALUES (?, ?)',
                    (target_id, amount)
                )
                await db_connection.commit()
                await update.message.reply_text(f"Баланс пользователя {target_id} установлен в {amount}.")
            else:
                async with db_connection.execute('SELECT balance FROM user_balances WHERE user_id = ?', (target_id,)) as cursor:
                    row = await cursor.fetchone()
                balance = row[0] if row else 0
                await update.message.reply_text(f"Баланс пользователя {target_id}: {balance}.")
        except ValueError:
            await update.message.reply_text("Неверный формат ID или суммы.")
    
    elif command == "games":
        await update.message.reply_text(f"Активных игр: {len(active_games)}")
    
    elif command == "cancel":
        if len(context.args) < 2:
            await update.message.reply_text("Использование: /admin cancel <chat_id>")
            return
            
        try:
            chat_id = int(context.args[1])
            if chat_id in active_games:
                game = active_games[chat_id]
                for player_id in game.players:
                    if not game.players[player_id]['folded']:
                        await game.update_user_balance(player_id, game.bet_amount, "game_admin_cancel")
                del active_games[chat_id]
                await update.message.reply_text(f"Игра в чате {chat_id} отменена.")
            else:
                await update.message.reply_text(f"Активная игра в чате {chat_id} не найдена.")
        except ValueError:
            await update.message.reply_text("Неверный формат chat_id.")
    elif command == "stats":
        await show_admin_stats(query, context)

async def show_admin_stats(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показ статистики"""
    try:
        async with db_connection.execute('SELECT COUNT(*) FROM user_balances') as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db_connection.execute('SELECT COUNT(*) FROM game_history') as cursor:
            total_games = (await cursor.fetchone())[0]
        async with db_connection.execute('SELECT SUM(amount) FROM transactions WHERE amount > 0') as cursor:
            total_deposits = (await cursor.fetchone())[0] or 0
        async with db_connection.execute('SELECT SUM(amount) FROM transactions WHERE amount < 0') as cursor:
            total_withdrawals = abs((await cursor.fetchone())[0] or 0)

        await query.edit_message_text(
            f"📊 <b>Статистика:</b>\n\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"🎮 Всего игр: {total_games}\n"
            f"💰 Общая сумма пополнений: {total_deposits} ₽\n"
            f"📤 Общая сумма выводов: {total_withdrawals} ₽",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['home']} Назад", callback_data="admin_menu")]])
        )
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await query.edit_message_text("❌ Произошла ошибка при загрузке статистики.")

async def on_startup(app: Application) -> None:
    global db_connection
    logger.info("Бот запущен")
    
    try:
        db_connection = await aiosqlite.connect(DATABASE_PATH)
        await db_connection.execute('''
            CREATE TABLE IF NOT EXISTS user_balances (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0
            )
        ''')
        await db_connection.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                transaction_type TEXT,
                game_id INTEGER,
                details TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db_connection.execute('''
            CREATE TABLE IF NOT EXISTS game_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                game_id INTEGER,
                bet_amount INTEGER,
                result TEXT,
                profit INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db_connection.execute('''
            CREATE TABLE IF NOT EXISTS withdrawal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                amount INTEGER,
                wallet TEXT,
                status TEXT DEFAULT 'pending',
                processed_by INTEGER,
                processed_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db_connection.commit()
        logger.info("Успешное подключение к SQLite")
    except Exception as e:
        logger.error(f"Ошибка подключения к SQLite: {e}")

async def on_shutdown(app: Application) -> None:
    logger.info("Бот остановлен")
    if db_connection:
        await db_connection.close()

def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("begin", begin_game))
    application.add_handler(CommandHandler("join", join_game))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("fast", start_fast_game))
    application.add_handler(CommandHandler("cancel", cancel_game))
    application.add_handler(CommandHandler("admin", admin_command))
    
    application.add_handler(CallbackQueryHandler(handle_deposit, pattern=r"^deposit$"))
    application.add_handler(CallbackQueryHandler(process_deposit, pattern=r"^deposit_\d+$"))
    application.add_handler(CallbackQueryHandler(handle_withdraw, pattern=r"^withdraw$"))
    application.add_handler(CallbackQueryHandler(handle_withdrawal_approval, pattern=r"^(approve|reject)_withdraw_\d+$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_withdrawal_message))
    
    application.add_handler(CallbackQueryHandler(handle_menu_callback, pattern=r"^(profile|balance|add_to_chat|top_players|info|back_to_menu|admin_menu)$"))
    application.add_handler(CallbackQueryHandler(handle_action, pattern=r"^(fold|call|check|show|look|swara|raise_\d+|confirm_show|decline_show|noop|join_swara|final_swara|final_split|final_continue)$"))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, private_start))
    application.add_handler(MessageHandler(filters.ALL, lambda u, c: None))
    
    application.post_init = on_startup
    application.post_stop = on_shutdown
    
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":

    main()