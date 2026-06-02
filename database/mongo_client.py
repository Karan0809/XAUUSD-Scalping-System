import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from uuid import uuid4

from pymongo import MongoClient as PyMongoClient
from pymongo.errors import ConfigurationError, ConnectionFailure, ServerSelectionTimeoutError

from config.settings import get_settings
from database.models import TradeRecord, SignalRecord, SessionMetrics

logger = logging.getLogger(__name__)


class MongoClient:
    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[PyMongoClient] = None
        self._db = None
        self._connected = False

    def connect(self) -> bool:
        if self._connected:
            return True
        try:
            self._client = PyMongoClient(
                self.settings.mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
            )
            self._client.admin.command("ping")
            self._db = self._client[self.settings.mongo_db]
            self._connected = True
            logger.info(f"Connected to MongoDB: {self.settings.mongo_db}")
            return True
        except (ConfigurationError, ConnectionFailure, ServerSelectionTimeoutError) as e:
            logger.warning(f"MongoDB connection failed: {e}")
            self._client = None
            self._db = None
            return False

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            self._connected = False
            logger.info("Disconnected from MongoDB")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _ensure_connection(self) -> bool:
        if not self._connected:
            return self.connect()
        return True

    def save_trade(self, trade: Any) -> bool:
        if not self._ensure_connection():
            logger.warning("MongoDB unavailable, trade not saved")
            return False
        try:
            collection = self._db[self.settings.mongo_trades_collection]
            data = trade.model_dump() if hasattr(trade, "model_dump") else trade
            if "outcome" not in data or data.get("outcome") is None:
                if data.get("profit") is not None:
                    data["outcome"] = "win" if data["profit"] > 0 else "loss"
            collection.update_one(
                {"trade_id": data.get("trade_id", str(uuid4()))},
                {"$set": data},
                upsert=True,
            )
            logger.debug(f"Trade saved: {data.get('trade_id')}")
            return True
        except Exception as e:
            logger.error(f"Failed to save trade: {e}")
            return False

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        if not self._ensure_connection():
            return None
        try:
            collection = self._db[self.settings.mongo_trades_collection]
            return collection.find_one({"trade_id": trade_id})
        except Exception as e:
            logger.error(f"Failed to get trade {trade_id}: {e}")
            return None

    def get_trades_by_date(self, date: str) -> List[Dict[str, Any]]:
        if not self._ensure_connection():
            return []
        try:
            collection = self._db[self.settings.mongo_trades_collection]
            return list(collection.find({"session_date": date}))
        except Exception as e:
            logger.error(f"Failed to get trades for {date}: {e}")
            return []

    def get_all_trades(self) -> List[Dict[str, Any]]:
        if not self._ensure_connection():
            return []
        try:
            collection = self._db[self.settings.mongo_trades_collection]
            return list(collection.find().sort("open_time", -1))
        except Exception as e:
            logger.error(f"Failed to get all trades: {e}")
            return []

    def save_signal(self, signal: Any) -> bool:
        if not self._ensure_connection():
            return False
        try:
            collection = self._db[self.settings.mongo_signals_collection]
            data = signal.model_dump() if hasattr(signal, "model_dump") else signal
            collection.update_one(
                {"signal_id": data.get("signal_id", str(uuid4()))},
                {"$set": data},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save signal: {e}")
            return False

    def save_metrics(self, metrics: SessionMetrics) -> bool:
        if not self._ensure_connection():
            return False
        try:
            collection = self._db[self.settings.mongo_metrics_collection]
            data = metrics.model_dump() if hasattr(metrics, "model_dump") else metrics
            collection.update_one(
                {"date": data.get("date", "")},
                {"$set": data},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save metrics: {e}")
            return False

    def get_performance_summary(self) -> Dict[str, Any]:
        if not self._ensure_connection():
            return {}
        try:
            trades_col = self._db[self.settings.mongo_trades_collection]
            total = trades_col.count_documents({})
            wins = trades_col.count_documents({"outcome": "win"})
            losses = trades_col.count_documents({"outcome": "loss"})
            pipeline = [
                {"$group": {
                    "_id": None,
                    "total_profit": {"$sum": {"$ifNull": ["$profit", 0]}},
                    "avg_profit": {"$avg": {"$ifNull": ["$profit", 0]}},
                    "max_profit": {"$max": {"$ifNull": ["$profit", 0]}},
                    "min_profit": {"$min": {"$ifNull": ["$profit", 0]}},
                }}
            ]
            agg = list(trades_col.aggregate(pipeline))
            stats = agg[0] if agg else {}
            return {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total * 100), 2) if total > 0 else 0,
                "total_profit": round(stats.get("total_profit", 0), 2),
                "avg_profit": round(stats.get("avg_profit", 0), 2),
                "max_profit": round(stats.get("max_profit", 0), 2),
                "min_profit": round(stats.get("min_profit", 0), 2),
            }
        except Exception as e:
            logger.error(f"Failed to get performance summary: {e}")
            return {}
